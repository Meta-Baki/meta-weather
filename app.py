from flask import Flask, request, jsonify, send_from_directory
import json
import requests
import os
from datetime import datetime
from zoneinfo import ZoneInfo
import base64


def update_github(history):
    """Writes history only to the data branch and reports GitHub errors."""
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not configured in Render")

    url = f"https://api.github.com/repos/{REPO}/contents/{FILE}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    existing = requests.get(url, headers=headers, params={"ref": HISTORY_BRANCH}, timeout=15)
    sha = None
    if existing.status_code == 200:
        sha = existing.json()["sha"]
    elif existing.status_code != 404:
        existing.raise_for_status()

    content = base64.b64encode(
        json.dumps(history, ensure_ascii=False, indent=2, sort_keys=True).encode()
    ).decode()
    payload = {"message": "update weather history", "content": content, "branch": HISTORY_BRANCH}
    if sha:
        payload["sha"] = sha

    saved = requests.put(url, json=payload, headers=headers, timeout=15)
    saved.raise_for_status()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO = "Meta-Baki/meta_baki_az"
FILE = "history.json"
HISTORY_BRANCH = "history-data"

BAKU_TZ = ZoneInfo("Asia/Baku")

# вњ”пёЏ Р’РђР–РќРћР• РРЎРџР РђР’Р›Р•РќРР• (РЎРўРђРўРРљРђ)
app = Flask(__name__, static_folder=".", static_url_path="")


# ---------------- MANIFEST ----------------
@app.route("/manifest.json")
def manifest():
    return send_from_directory(".", "manifest.json")


# ---------------- SERVICE WORKER ----------------
@app.route("/sw.js")
def service_worker():
    return send_from_directory(".", "sw.js")


DATA_FILE = "data.json"
HISTORY_FILE = "history.json"
LAST_SAVE_FILE = "last_save.json"


# ---------------- HOME ----------------
@app.route("/")
def home():
    return send_from_directory(".", "index.html")


# ---------------- HISTORY STORAGE ----------------
_HISTORY_CACHE = None


def make_history_entry(entry):
    now = datetime.now(BAKU_TZ)
    return {
        "timestamp": now.isoformat(),
        "time": now.strftime("%d.%m %H:%M:%S"),
        "temp": float(entry.get("temp", 0)),
        "wind": round(float(entry.get("wind_ms", 0)) * 3.6, 1),
        "gust": round(float(entry.get("wind_gust_ms", 0)) * 3.6, 1),
        "humidity": float(entry.get("humidity", 0)),
        "pressure": float(entry.get("pressure", 0)),
        "rain": float(entry.get("rain_1h", 0))
    }


def normalise_history(data):
    normalised = []

    for item in data:
        if not isinstance(item, dict):
            continue

        # Old entries used ISO time in `time` instead of `timestamp`.
        timestamp = item.get("timestamp") or item.get("time")
        if not isinstance(timestamp, str) or "T" not in timestamp:
            continue

        try:
            point_time = datetime.fromisoformat(timestamp)
        except ValueError:
            continue

        normalised.append({
            "timestamp": timestamp,
            "time": item.get("time") if "T" not in str(item.get("time", "")) else point_time.strftime("%d.%m %H:%M:%S"),
            "temp": float(item.get("temp", 0)),
            "wind": float(item.get("wind", 0)),
            "gust": float(item.get("gust", 0)),
            "humidity": float(item.get("humidity", 0)),
            "pressure": float(item.get("pressure", 0)),
            "rain": float(item.get("rain", 0))
        })

    return normalised

def load_history():
    """Loads persistent graph points from the history-data branch."""
    global _HISTORY_CACHE

    if _HISTORY_CACHE is not None:
        return _HISTORY_CACHE.copy()

    data = []
    try:
        response = requests.get(
            f"https://raw.githubusercontent.com/{REPO}/{HISTORY_BRANCH}/{FILE}",
            timeout=15
        )
        if response.status_code == 200:
            candidate = response.json()
            if isinstance(candidate, list):
                data = candidate
    except Exception:
        pass

    _HISTORY_CACHE = normalise_history(data)[-2000:]
    return _HISTORY_CACHE.copy()


def last_history_timestamp():
    history = load_history()
    if not history:
        return None
    try:
        return datetime.fromisoformat(history[-1]["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None

def save_history(entry):
    global _HISTORY_CACHE

    data = load_history()
    data.append(make_history_entry(entry))
    _HISTORY_CACHE = data[-2000:]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(_HISTORY_CACHE, f, ensure_ascii=False, indent=2)

    update_github(_HISTORY_CACHE)


# ---------------- UPDATE ----------------
@app.route("/update", methods=["POST"])
def update():
    try:
        data = request.get_json(force=True)

        if not data:
            return {"error": "No JSON"}, 400

        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        if can_save():
            save_history(data)

            with open(LAST_SAVE_FILE, "w", encoding="utf-8") as f:
                json.dump({"time": datetime.now(BAKU_TZ).timestamp()}, f)

        return {"ok": True}

    except Exception as e:
        return {"error": str(e)}, 500


# ---------------- STATION ----------------
@app.route("/station")
def station():

    if not os.path.exists(DATA_FILE):
        return jsonify({
            "temp": 0,
            "humidity": 0,
            "wind_ms": 0,
            "wind_gust_ms": 0,
            "pressure": 0,
            "rain_1h": 0,
            "rain_24h": 0
        })

    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)

        return jsonify(data)

    except Exception as e:
        return jsonify({"error": str(e)})


# ---------------- HISTORY ----------------
@app.route("/history")
def history():
    return jsonify(load_history())


# ---------------- FLAT HISTORY ----------------
@app.route("/history_flat")
def history_flat():
    return jsonify(load_history())


# ---------------- 30 MIN CHECK ----------------
def can_save():
    # The last point is stored in GitHub, so this survives deploys and restarts.
    last = last_history_timestamp()
    if last is None:
        return True

    return (datetime.now(BAKU_TZ) - last).total_seconds() >= 1800

# ---------------- FORECAST 7 ----------------
@app.route("/forecast7")
def forecast7():

    try:

        # Общий бесплатный прогнозный API Open-Meteo.
        # Он используется и для 14-дневного прогноза на странице.
        url = "https://api.open-meteo.com/v1/forecast"

        params = {

            "latitude": 40.379228,
            "longitude": 49.9625323,

            "daily":
                "weather_code,"
                "temperature_2m_max,"
                "temperature_2m_min,"
                "apparent_temperature_max,"
                "apparent_temperature_min,"
                "precipitation_probability_max,"
                "wind_speed_10m_mean,"
                "cloud_cover_mean,"
                "uv_index_max,"
                "sunrise,"
                "sunset,"
                "wind_speed_10m_max,"
                "wind_direction_10m_dominant,"
                "wind_gusts_10m_max,"
                "surface_pressure_mean,"
                "precipitation_sum",

            "hourly":
                "temperature_2m,"
                "weather_code,"
                "relative_humidity_2m,"
                "wind_speed_10m,"
                "surface_pressure,"
                "dew_point_2m,"
                "visibility",

            "forecast_days": 7,

            "timezone": "Asia/Baku"
        }

        r = requests.get(url, params=params, timeout=20)

        if r.status_code != 200:
            return jsonify({
                "error": "ECMWF API error",
                "status": r.status_code,
                "response": r.text
            })

        raw = r.json()

        if "daily" not in raw:
            return jsonify({
                "error": "No daily data",
                "response": raw
            })

        data = {

            "daily": {

                "time": raw["daily"]["time"],

                "weathercode":
                    raw["daily"]["weather_code"],

                "temperature_2m_max":
                    raw["daily"]["temperature_2m_max"],

                "temperature_2m_min":
                    raw["daily"]["temperature_2m_min"],

                "apparent_temperature_max":
                    raw["daily"]["apparent_temperature_max"],

                "apparent_temperature_min":
                    raw["daily"]["apparent_temperature_min"],

                "precipitation_probability_max":
                    raw["daily"]["precipitation_probability_max"],

                "wind_speed_10m_mean":
                    raw["daily"]["wind_speed_10m_mean"],

                "cloud_cover_mean":
                    raw["daily"]["cloud_cover_mean"],

                "uv_index_max":
                    raw["daily"]["uv_index_max"],

                "sunrise":
                    raw["daily"]["sunrise"],

                "sunset":
                    raw["daily"]["sunset"],

                "precipitation_sum":
                    raw["daily"]["precipitation_sum"],

                "windspeed_10m_max":
                    raw["daily"]["wind_speed_10m_max"],

                "winddirection_10m_dominant":
                    raw["daily"]["wind_direction_10m_dominant"],

                "windgusts_10m_max":
                    raw["daily"]["wind_gusts_10m_max"],

                "surface_pressure_mean":
                    raw["daily"]["surface_pressure_mean"]
            },

            "hourly": {

                "time":
                    raw["hourly"]["time"],

                "temperature_2m":
                    raw["hourly"]["temperature_2m"],

                "weathercode":
                    raw["hourly"]["weather_code"],

                "relative_humidity_2m":
                    raw["hourly"]["relative_humidity_2m"],

                "wind_speed_10m":
                    raw["hourly"]["wind_speed_10m"],

                "surface_pressure":
                    raw["hourly"]["surface_pressure"],

                "dew_point_2m":
                    raw["hourly"]["dew_point_2m"],

                "visibility":
                    raw["hourly"]["visibility"]
            }
        }

        return jsonify(data)

    except Exception as e:
        return jsonify({"error": str(e)})


# ---------------- WARNING ----------------
@app.route("/warning")
def warning():

    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)

    except:
        return jsonify([])

    warnings = []

    if d.get("wind_ms", 0) * 3.6 > 40:
        warnings.append("вљ пёЏ GГјclГј kГјlЙ™k gГ¶zlЙ™nilir")

    if d.get("humidity", 0) > 90:
        warnings.append("рџЊ« Duman ehtimalД± yГјksЙ™kdir")

    if d.get("rain_1h", 0) > 0:
        warnings.append("рџЊ§ YaДџД±Еџ mГјЕџahidЙ™ olunur")

    return jsonify(warnings)


# ---------------- STATUS ----------------
@app.route("/status")
def status():

    if not os.path.exists(DATA_FILE):
        return jsonify({"status": "offline"})

    last = os.path.getmtime(DATA_FILE)
    now = datetime.now(BAKU_TZ).timestamp()

    if now - last > 300:
        return jsonify({"status": "offline"})

    return jsonify({"status": "online"})


# ---------------- STORY ----------------
@app.route("/history_content")
def history_content():

    text = ""

    try:
        with open("story/history.txt", "r", encoding="utf-8") as f:
            text = f.read()
    except:
        text = "HekayЙ™ tapД±lmadД±."

    image_path = "/story/image.jpg"

    if os.path.exists("story/image.png"):
        image_path = "/story/image.png"

    return jsonify({"text": text, "image": image_path})


@app.route("/space_content")
def space_content():

    folder = "story/space"
    news = []

    try:
        files = sorted(
            [f for f in os.listdir(folder) if f.endswith(".txt")]
        )

        for file in files:

            name = os.path.splitext(file)[0]

            image = None

            if os.path.exists(f"{folder}/{name}.jpg"):
                image = f"/story/space/{name}.jpg"
            elif os.path.exists(f"{folder}/{name}.png"):
                image = f"/story/space/{name}.png"

            with open(f"{folder}/{file}", "r", encoding="utf-8") as f:
                text = f.read()

            news.append({
                "text": text,
                "image": image
            })

    except:
        pass

    return jsonify(news)


@app.route("/records_content")
def records_content():

    base_dir = os.path.dirname(os.path.abspath(__file__))
    records_dir = os.path.join(base_dir, "story", "records")

    text_path = os.path.join(records_dir, "records.txt")
    jpg_path = os.path.join(records_dir, "records.jpg")
    png_path = os.path.join(records_dir, "records.png")

    # РўРµРєСЃС‚
    if os.path.isfile(text_path):
        try:
            with open(text_path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            text = f"РћС€РёР±РєР° С‡С‚РµРЅРёСЏ records.txt: {e}"
    else:
        text = "Rekord mЙ™lumatlarД± tapД±lmadД±."

    # РР·РѕР±СЂР°Р¶РµРЅРёРµ
    image_path = None

    if os.path.isfile(jpg_path):
        image_path = "/story/records/records.jpg"
    elif os.path.isfile(png_path):
        image_path = "/story/records/records.png"

    return jsonify({
        "text": text,
        "image": image_path
    })


@app.route("/meta_content")
def meta_content():

    folder = "story/meta"
    news = []

    try:
        files = sorted(
            [f for f in os.listdir(folder) if f.endswith(".txt")]
        )

        for file in files:

            name = os.path.splitext(file)[0]

            image = None

            if os.path.exists(f"{folder}/{name}.jpg"):
                image = f"/story/meta/{name}.jpg"
            elif os.path.exists(f"{folder}/{name}.png"):
                image = f"/story/meta/{name}.png"

            with open(f"{folder}/{file}", "r", encoding="utf-8") as f:
                text = f.read()

            news.append({
                "text": text,
                "image": image
            })

    except:
        pass

    return jsonify(news)


@app.route('/story/<path:filename>')
def story_files(filename):
    return send_from_directory('story', filename)


@app.route("/test")
def test():
    return {"status": "ok"}


@app.route("/robots.txt")
def robots():
    return app.response_class(
        """User-agent: *
Allow: /

Sitemap: https://meta-baki1.onrender.com/sitemap.xml
""",
        mimetype="text/plain"
    )


@app.route("/sitemap.xml")
def sitemap():
    return send_from_directory(".", "sitemap.xml")


# ---------------- RUN ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False) 
