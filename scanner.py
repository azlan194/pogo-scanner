import requests
import blackboxprotobuf
import datetime
import json
import os
import math
import time
import argparse
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse
import threading
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# --- INITIALIZE ENVIRONMENT VARIABLES ---
load_dotenv()  # This looks for a local .env file and loads its variables into system memory

# --- CONFIGURATION ---
DEFAULT_LAT = 37.640220
DEFAULT_LON = -122.423450  # San Bruno Area
CACHE_FILE = "spawns_cache.json"
HTML_FILE = "radar.html"
PORT = 8080
POLL_DELAY_SECONDS = 6
API_FETCH_URL = "https://api2.flymego.io/Coords/WildEncounter"
API_HISTORY_URL = "https://api2.flymego.io/History/GetWildEncounter"
API_CLEAR_URL = "https://api2.flymego.io/History/ClearWildEncounter"
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
FLYMEGO_TOKEN = os.getenv("FLYMEGO_AUTH_TOKEN")

with open('pokemon_id_map.json') as fi:
    poke_map = json.load(fi)['pokemon']
    POKEDEX = {int(k): v['name'] for k, v in poke_map.items()}
    POKEDEX_flip = {name: id for id, name in POKEDEX.items()}

with open('level_map.json') as fi:
    LEVEL_MAP = json.load(fi)

HEADERS = {
    "authorization": FLYMEGO_TOKEN if FLYMEGO_TOKEN else "Bearer MISSING_TOKEN",
    "content-type": "application/x-protobuf",
    "accept-encoding": "gzip",
    "user-agent": "okhttp/4.12.0"
}

# TEMPLATE_BYTES = b'\x0a$49edd87a-3ff3-49fd-b674-5ced81c0fafe\x12\x15\x18\x01 -(-0\x01=\xd4}\xc0=EYQ;?\xf0\x01\x01 \x02*\x1b\t`\x1f\x9d\xba\xf2\xd1B@\x11p_\x07\xce\x19\x9b^\xc0\x19\x00\x00\x00\x00\x00\x88\xc3@2\x18\t\xd8\xd4yT\xfc\xb9B@\x11\xb5\xcbm\xb1\x11{^\xc0\x18\x8b\xb6\xee\xfc\x11A\xa9\x93"\xe8\x1etC@I\x00\x08\x0e\x1e\\BS\xc0'
TEMPLATE_BYTES = b'\x0a$7fcc3327-0cc4-4896-b2ca-eec208eb96ae\x12\x13\x18\x01 -(-=\xd4}\xc0=EYQ;?\xf0\x01\x01 \x02*\x1b\t`\x1f\x9d\xba\xf2\xd1B@\x11p_\x07\xce\x19\x9b^\xc0\x19\x00\x00\x00\x00\x00\x88\xb3@2\x18\t\x0f\xf2z0)\xdbB@\x11Ey\x8b,>\x9e^\xc0\x18\x82\xca\xe5\xb5\rA\x97VC\xe2\x1etC@I\x95\xc7\xe4\x1e\\BS\xc0'


# --- NATIVE ANDROID GPS EXTRACTION ---
def get_android_gps(silent=False):
    """Queries the Termux:API binary for real-time hardware GPS coordinates."""
    if not silent:
        print("[*] Requesting hardware GPS fix from Android...")
    try:
        result = subprocess.run(
            ['termux-location', '-p', 'gps'],
            capture_output=True, text=True, timeout=8
        )
        if result.returncode == 0:
            location_data = json.loads(result.stdout)
            lat = location_data.get('latitude')
            lon = location_data.get('longitude')
            if lat and lon:
                if not silent:
                    print(f"[✔] Location Acquired: {lat}, {lon} (Accuracy: {location_data.get('accuracy')}m)")
                return lat, lon
    except Exception:
        pass
    return DEFAULT_LAT, DEFAULT_LON


# --- GOOGLE MAPS TRAFFIC ENGINE ---
def get_driving_info(origin_lat, origin_lon, dest_lat, dest_lon):
    """Queries Google Maps API for driving time under real-time traffic conditions."""
    if not GOOGLE_MAPS_API_KEY or GOOGLE_MAPS_API_KEY == "YOUR_GOOGLE_MAPS_API_KEY":
        return None, "No API Key"

    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins": f"{origin_lat},{origin_lon}",
        "destinations": f"{dest_lat},{dest_lon}",
        "mode": "driving",
        "key": GOOGLE_MAPS_API_KEY
    }

    try:
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            res_json = response.json()
            if res_json.get("status") == "OK":
                element = res_json["rows"][0]["elements"][0]
                if element.get("status") == "OK":
                    duration_seconds = element["duration"]["value"]
                    duration_text = element["duration"]["text"]
                    return duration_seconds, duration_text
    except Exception as e:
        print(f"[-] Google Maps API error: {e}")

    return None, "N/A"


# --- GEOGRAPHIC UTILITIES ---
def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(R * c, 1)


def calculate_bearing(lat1, lon1, lat2, lon2):
    """Calculates the initial compass bearing from Point 1 to Point 2 in degrees."""
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lon_rad = math.radians(lon2 - lon1)

    y = math.sin(delta_lon_rad) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(delta_lon_rad)

    initial_bearing_rad = math.atan2(y, x)
    initial_bearing_deg = math.degrees(initial_bearing_rad)

    # Normalize the angle to a standard 0-360 compass degree
    compass_bearing = (initial_bearing_deg + 360) % 360
    return int(compass_bearing)


# --- CACHE PERSISTENCE ENGINE ---
def load_local_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_local_cache(cache_dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache_dict, f, indent=4)


def clean_expired_entries(cache_dict):
    now_ms = int(datetime.datetime.now(ZoneInfo("America/Los_Angeles")).timestamp() * 1000)
    cleaned_cache = {k: v for k, v in cache_dict.items() if v.get("despawn_ms", 0) > now_ms}
    expired_count = len(cache_dict) - len(cleaned_cache)
    if expired_count > 0:
        print(f"[*] Cleaned {expired_count} expired entries out of local cache.")
    save_local_cache(cleaned_cache)
    return cleaned_cache


# --- NETWORK & PARSING ---
def create_payload(lat, lon, radius):
    message, typedef = blackboxprotobuf.decode_message(TEMPLATE_BYTES)
    typedef['5']['message_typedef']['1']['type'] = 'double'
    typedef['5']['message_typedef']['2']['type'] = 'double'
    typedef['5']['message_typedef']['3']['type'] = 'double'
    message['5']['1'] = lat
    message['5']['2'] = lon
    message['5']['3'] = radius
    return blackboxprotobuf.encode_message(message, typedef)


def fetch_spawns(payload_bytes):
    url = API_FETCH_URL
    try:
        response = requests.post(url, headers=HEADERS, data=payload_bytes, timeout=10)
        if response.status_code == 200:
            return response.content
        print(f"[-] HTTP Error {response.status_code}: Bearer Token Expired. Please update token and Template Bytes.")
        return None
    except Exception as e:
        print(f"[-] Network error: {e}")
        return None


def fetch_and_populate_history(current_lat, current_lon):
    """Phase 2: The Data Master. Pulls History API, grabs stats, and builds the WebUI cache."""
    url = API_HISTORY_URL
    payload_bytes = TEMPLATE_BYTES  # Adjust if the History endpoint requires a specific payload

    try:
        print("\n[*] Pulling complete historical payload for WebUI...")
        response = requests.post(url, headers=HEADERS, data=payload_bytes, timeout=10)

        if response.status_code != 200 or not response.content:
            print("[-] History API failed or returned empty.")
            return False

        message, typedef = blackboxprotobuf.decode_message(response.content)
        if isinstance(message['2'], dict):
            message['2'] = [message['2']]

        # TERMINATION TRIGGER: Server key '2' missing or empty means queue is exhausted
        if '2' not in message or not message['2']:
            return False, 0

        if '2' in typedef and 'message_typedef' in typedef['2']:
            typedef['2']['message_typedef']['2']['type'] = 'double'
            typedef['2']['message_typedef']['3']['type'] = 'double'
            typedef['2']['message_typedef']['10']['type'] = 'double'

        clean_message, _ = blackboxprotobuf.decode_message(response.content, typedef)

        encounters = clean_message.get('2', [])
        if isinstance(encounters, dict):
            encounters = [encounters]

        radar_cache = load_local_cache()
        radar_cache = clean_expired_entries(radar_cache)
        new_additions = 0

        for spawn in encounters:
            enc_id = spawn.get('1', b'unknown_id')
            if isinstance(enc_id, bytes):
                enc_id = enc_id.decode('utf-8', errors='ignore')

            # Only process and call Google Maps if this is a brand new spawn
            if enc_id not in radar_cache:
                # ⚠️ CRITICAL: Map these integer keys to match your History Protobuf dump
                pkmn_id = spawn.get('5', {}).get('1', 1)
                pkmn_name = POKEDEX.get(pkmn_id, f"Unknown ({pkmn_id})")
                lat = float(spawn.get('2', 0.0))
                lon = float(spawn.get('3', 0.0))
                atk = int(spawn.get('6', 0))
                dfn = int(spawn.get('7', 0))
                sta = int(spawn.get('8', 0))
                cp = int(spawn.get('9', 0))
                lvl_str = str(spawn.get('10', 0))[:5]
                lvl = LEVEL_MAP.get(lvl_str, 0)

                despawn_ms = int(spawn.get('12', 0))
                time_str = "Unknown"
                if despawn_ms:
                    now = datetime.datetime.now(ZoneInfo("America/Los_Angeles")).timestamp() * 1000
                    if despawn_ms < now:
                        print(f"{pkmn_name} has EXPIRED")
                        continue
                    dt_obj = datetime.datetime.fromtimestamp(despawn_ms / 1000.0, tz=ZoneInfo("America/Los_Angeles"))
                    time_str = dt_obj.strftime('%I:%M:%S %p PDT')

                # 🚨 GOOGLE MAPS API CALL HAPPENS HERE (Saves your billing quota!) 🚨
                drive_seconds, drive_text = get_driving_info(current_lat, current_lon, lat, lon)

                radar_cache[enc_id] = {
                    "encounter_id": enc_id,
                    "pokemon_id": pkmn_id,
                    "name": pkmn_name,
                    "latitude": lat,
                    "longitude": lon,
                    "despawn_time": time_str,
                    "despawn_ms": despawn_ms,
                    "atk": atk,
                    "def": dfn,
                    "sta": sta,
                    "stats": '/'.join([str(atk), str(dfn), str(sta)]),
                    "lvl": lvl,
                    "cp": cp,
                    "maps_url": f"https://www.google.com/maps/search/?api=1&query={lat},{lon}",
                    "drive_seconds": drive_seconds,
                    "drive_text": drive_text
                }
                new_additions += 1

        if new_additions > 0:
            save_local_cache(radar_cache)
            print(f"[+] Added {new_additions} fully enriched targets to the WebUI.")
        else:
            print("[*] History data pulled, but no new targets found.")

        return True

    except Exception as e:
        print(f"[-] Error parsing History payload: {e}")

    return False


def process_radar_feed(raw_response_bytes, current_lat, current_lon):
    """Phase 1: Pure Discovery. Wakes up the map but does NOT save data."""
    if not raw_response_bytes:
        return False, 0, 0

    message, typedef = blackboxprotobuf.decode_message(raw_response_bytes)

    # TERMINATION TRIGGER: Key '2' missing means the queue is exhausted
    if '2' not in message or not message['2']:
        return False, 0, 0

    if '2' in typedef and 'message_typedef' in typedef['2']:
        typedef['2']['message_typedef']['2']['type'] = 'double'
        typedef['2']['message_typedef']['3']['type'] = 'double'

    clean_message, _ = blackboxprotobuf.decode_message(raw_response_bytes, typedef)

    encounter = clean_message.get('2', {})
    dist = 0
    if encounter:
        lat = encounter.get('2', 0.0)
        lon = encounter.get('3', 0.0)
        dist = calculate_distance(current_lat, current_lon, lat, lon)

        batch_count = 1
    else:
        batch_count = 0
    # We no longer cache anything here. We just return the count to keep the UI text updated.
    return batch_count > 0, batch_count, dist


# --- ASYNC MULTI-THREADED SCANNER ENGINE ---
def run_radar_scan_cycle(lat, lon, radius, pkmn_id, server_instance=None):
    """Executes map scraping loop. Updates live thread states if attached to server."""
    if server_instance:
        server_instance.scan_in_progress = True
        server_instance.spawns_found_current = 0
        server_instance.scan_step_message = "Initializing radar array..."

    print(f"\n📡 [API] Requesting fresh radar data frame (Radius: {int(radius)}m)...")
    call_count = 1
    while True:
        timestamp = datetime.datetime.now().strftime('%H:%M:%S')

        # 1. Pre-request feedback
        print(f"[{timestamp}] 🔄 Loop #{call_count}: Sending discovery request to Coords API...")
        if server_instance:
            server_instance.scan_step_message = f"Querying batch #{call_count}..."

        payload = create_payload(lat, lon, radius)
        response_data = fetch_spawns(payload)
        has_more_data, batch_count, dist = process_radar_feed(response_data, lat, lon)

        if server_instance:
            server_instance.spawns_found_current += batch_count
            server_instance.scan_step_message = f"Batch #{call_count}: Found {server_instance.spawns_found_current} targets..."

        if not has_more_data:
            print("[✔] Map space update complete.")
            break
        # 3. Inter-loop wait feedback
        print(f"[{timestamp}] 💡 Found pokemon spawned with distance: {dist}m")
        print(f"[{timestamp}] 💤 Sleeping for {POLL_DELAY_SECONDS} seconds before starting Loop #{call_count + 1}...\n")
        time.sleep(POLL_DELAY_SECONDS)
        call_count += 1

    if server_instance:
        server_instance.scan_in_progress = False


# --- DYNAMIC HTML RENDER ENGINE ---
def generate_cards_html(current_lat, current_lon):
    """Generates strictly the HTML for the target cards. Used by both full page load and AJAX updates."""
    radar_cache = load_local_cache()
    radar_cache = clean_expired_entries(radar_cache)

    # DYNAMIC METRICS: Calculate live distance and bearing angle
    for enc_id, target in radar_cache.items():
        target['live_distance'] = calculate_distance(current_lat, current_lon, target['latitude'], target['longitude'])
        target['bearing'] = calculate_bearing(current_lat, current_lon, target['latitude'], target['longitude'])

    sorted_list = sorted(radar_cache.values(), key=lambda x: x['live_distance']) if radar_cache else []

    html = [f"<h2>🟢 Active Mobile Targets ({len(sorted_list)})</h2>"]

    if not sorted_list:
        html.append("<p style='color:#777;'>🏜️ No targets found within range. Click the Scan banner above!</p>")

    for index, target in enumerate(sorted_list, 1):
        now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
        seconds_left = max(0, int((target['despawn_ms'] - now_ms) / 1000))

        drive_seconds = target.get('drive_seconds')
        drive_text = target.get('drive_text', 'N/A')

        is_catchable = drive_seconds is not None and seconds_left > (drive_seconds + 60)
        border_class = "green" if is_catchable else "red"
        html_status = f"🚗 Catchable! ({drive_text} drive)" if is_catchable else f"⚠️ Too Late! ({drive_text} drive)"

        # NEW: Conditional Stats Badge Rendering
        if 'stats' in target:
            iv_str = f"⭐ 100% IV ({target['stats']}) • Lvl {target['lvl']}"
            stats_html = f"<div style='color:#FFD54F; font-weight:bold; margin-bottom:4px;'>{iv_str}</div>"
        else:
            stats_html = "<div style='color:#888; font-style:italic; margin-bottom:4px;'>📊 Stats Unscanned (Click Fetch Stats)</div>"

        maps_url = target.get('maps_url',
                              f"https://www.google.com/maps/search/?api=1&query={target['latitude']},{target['longitude']}")
        card_dom_id = f"card_{target['encounter_id']}"

        html.append(f"""
                <div class="card {border_class}" id="{card_dom_id}">
                    <div class="name">
                        #{index}. {target['name']} 
                        <span style="font-weight:normal; font-size:16px;">
                            ({target['live_distance']}m 
                            <span style="display:inline-block; transform:rotate({target['bearing']}deg); margin-left:4px;">⬆️</span>)
                        </span>
                    </div>
                    {stats_html}
                    <div><b>Status:</b> {html_status}</div>
                    <div><b>Time Left:</b> <span class="live-timer" id="timer_{index}" data-card-id="{card_dom_id}" data-time="{target['despawn_ms']}">Calculating...</span></div>
                    <div><b>CP:</b> {target.get('cp', "Unknown")}</div>
                    <div><b>Level:</b> {target.get('lvl', "Unknown")}</div>
                    <div><b>Stats:</b> {target.get('stats', "Unknown")}</div>
                    <div><b>Despawn Time:</b> {target['despawn_time']}</div>
                    <div class="btn-box">
                        <a class="btn btn-maps" href="{maps_url}">🗺️ Open Maps</a>
                        <button class="btn btn-caught" onclick="markAsCaught('{card_dom_id}', '{target['encounter_id']}')">✨ Caught</button>
                    </div>
                </div>
                """)
    return "\n".join(html)


# --- DYNAMIC HTML RENDER ENGINE ---
def generate_dashboard_html(current_lat, current_lon, current_radius):
    html = [
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>",
        "<title>Radar Desk</title>",
        "<style>",
        "  body { font-family: Arial, sans-serif; background: #121212; color: #E0E0E0; padding: 15px; margin: 0; }",
        "  .controls-row { display: flex; gap: 10px; margin-bottom: 20px; }",
        "  .radius-input { flex: 1; padding: 15px; border-radius: 10px; background: #1E1E1E; color: #FFF; border: 1px solid #333; font-size: 16px; font-weight: bold; text-align: center; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }",
        "  .radius-input:disabled { opacity: 0.5; cursor: not-allowed; }",
        "  .scan-banner { flex: 2; background: #2196F3; padding: 15px; border-radius: 10px; text-align: center; font-weight: bold; color: #fff; cursor: pointer; border: none; font-size: 16px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); margin-bottom: 0; }",
        "  .scan-banner:disabled { background: #555 !important; color: #aaa; cursor: not-allowed; box-shadow: none; }",
        "  .card { background: #1E1E1E; padding: 15px; margin-bottom: 12px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); transition: 0.3s; }",
        "  .green { border-left: 6px solid #4CAF50; } .red { border-left: 6px solid #F44336; }",
        "  .name { font-size: 18px; font-weight: bold; color: #FFF; margin-bottom: 5px; }",
        "  .btn-box { display: flex; gap: 10px; margin-top: 12px; }",
        "  .btn { flex: 1; padding: 12px; border-radius: 6px; text-decoration: none; font-weight: bold; text-align: center; border: none; cursor: pointer; font-size: 14px; }",
        "  .btn-maps { background: #00E676; color: #000; }",
        "  .btn-caught { background: #37474F; color: #FFD54F; }",
        "  .expired-text { color: #FF5252; font-weight: bold; }",
        "</style>",
        "<script>",
        "function triggerFetchStats() {",
        "    const btn = document.getElementById('fetchStatsBtn');",
        "    const originalText = btn.innerText;",
        "    btn.disabled = true;",
        "",
        "    // Updated text to reflect that this might take a few seconds",
        "    btn.innerText = '⏳ Pulling History & Maps...';",
        "",
        "    fetch('/fetch-stats', { method: 'POST' })",
        "    .then(res => {",
        "       if (res.ok) {",
        "         // The server has finished writing the cache. Force the browser to reload the page.",
        "         window.location.reload(); ",
        "       } else {",
        "         alert('Error fetching stats from server. Check your Termux console.');",
        "         btn.disabled = false;",
        "         btn.innerText = originalText;",
        "       }",
        "    })",
        "    .catch(() => {",
        "         alert('Network error. Could not reach the Python server.');",
        "         btn.disabled = false;",
        "         btn.innerText = originalText;",
        "    });",
        "  }",
        "/* LIVE TIMING ENGINE */",
        "function updateAllTimers() {",
        "  const now = Date.now();",
        "  document.querySelectorAll('.live-timer').forEach(timer => {",
        "    const despawnTime = parseInt(timer.getAttribute('data-time'));",
        "    const cardId = timer.getAttribute('data-card-id');",
        "    const cardElement = document.getElementById(cardId);",
        "    const remaining = despawnTime - now;",
        "",
        "    if (remaining <= 0) {",
        "      timer.innerHTML = '<span class=\"expired - text\">⚠️ Despawned!</span>';",
        "      if (cardElement && cardElement.classList.contains('green')) {",
        "        cardElement.classList.remove('green');",
        "        cardElement.classList.add('red');",
        "      }",
        "    } else {",
        "      const totalSecs = Math.floor(remaining / 1000);",
        "      const mins = Math.floor(totalSecs / 60);",
        "      const secs = totalSecs % 60;",
        "      timer.innerText = mins + 'm ' + secs + 's remaining';",
        "    }",
        "  });",
        "}",
        "",
        "function runLiveCountdowns() {",
        "  // Keep the 1-second interval moving normally",
        "  setInterval(updateAllTimers, 1000);",
        "}",
        "",
        "  /* HYBRID SILENT POLLING ENGINE */",
        "function pollLiveUpdates() {",
        "  const btn = document.getElementById('globalScanBtn');",
        "  const radiusInput = document.getElementById('radiusInput');",
        "  const container = document.getElementById('card-container');",
        "  ",
        "  const interval = setInterval(() => {",
        "    fetch('/live-update')",
        "    .then(res => res.json())",
        "    .then(data => {",
        "      /* 1. Update the DOM silently */",
        "      container.innerHTML = data.cards_html;",
        "      ",
        "      /* 2. INSTANT FIX: Force timers to calculate right now before the user sees \"Calculating...\" */",
        "      updateAllTimers();",
        "      ",
        "      if (data.in_progress) {",
        "        btn.disabled = true;",
        "        radiusInput.disabled = true;",
        "        btn.innerText = '🛰️ ' + data.message;",
        "      } else {",
        "        clearInterval(interval);",
        "        btn.disabled = false;",
        "        radiusInput.disabled = false;",
        "        btn.innerText = '🛰️ Scan for New Spawns';",
        "      }",
        "    })",
        "    .catch(() => clearInterval(interval));",
        "  }, 2500);",
        "}",
        "",
        "  function triggerGlobalScan() {",
        "    const btn = document.getElementById('globalScanBtn');",
        "    const radiusInput = document.getElementById('radiusInput');",
        "    const currentRadius = radiusInput.value || 5000;",
        "",
        "    btn.disabled = true;",
        "    radiusInput.disabled = true;",
        "    btn.innerText = '🛰️ Initializing worker...';",
        "",
        "    fetch('/scan?radius=' + encodeURIComponent(currentRadius), { method: 'POST' })",
        "    .then(res => {",
        "       if (res.ok) {",
        "         pollLiveUpdates();",
        "       } else {",
        "         alert('Scan failed to initialize.');",
        "         btn.disabled = false;",
        "         radiusInput.disabled = false;",
        "         btn.innerText = '🛰️ Scan for New Spawns';",
        "       }",
        "    });",
        "  }",
        "",
        "  function markAsCaught(cardId, encId) {",
        "    if(!confirm('Clear this spawn from map?')) return;",
        "    fetch('/caught?id=' + encodeURIComponent(encId), { method: 'POST' })",
        "    .then(res => {",
        "       if(res.ok) {",
        "         document.getElementById(cardId).style.transform = 'scale(0)';",
        "         setTimeout(() => document.getElementById(cardId).remove(), 300);",
        "       } else { alert('Error updating database.'); }",
        "    });",
        "  }",
        "",
        "  window.onload = function() {",
        "    runLiveCountdowns();",
        "    fetch('/live-update')",
        "    .then(res => res.json())",
        "    .then(data => { if(data.in_progress) pollLiveUpdates(); });",
        "  };",
        "</script>",
        "</head><body>",
        "",
        "<div class='controls-row'>",
        f"  <input type='number' id='radiusInput' class='radius-input' value='{int(current_radius)}' min='100' max='50000' placeholder='Meters'>",
        "  <button id='globalScanBtn' class='scan-banner' onclick='triggerGlobalScan()'>🛰️ Scan for Spawns</button>",
        "  <button id='fetchStatsBtn' class='scan-banner' style='background: #FF9800; flex: 1;' onclick='triggerFetchStats()'>📊 Fetch Stats</button>",
        "</div>",
        "",
        "",
        "<div id='card-container'>"
    ]

    html.append(generate_cards_html(current_lat, current_lon))
    html.append("</div></body></html>")
    return "\n".join(html)


# --- TERMUX LOCAL HOST SERVER INTERACTION ENGINE ---
class RadarRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return  # Suppress standard server spam logs inside the terminal window

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)

        if parsed_url.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            html_content = generate_dashboard_html(self.server.my_lat, self.server.my_lon, self.server.scan_radius)
            self.wfile.write(html_content.encode("utf-8"))

        elif parsed_url.path == "/live-update":
            # This endpoint feeds BOTH the UI status text AND the latest target cards
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()

            payload = {
                "in_progress": self.server.scan_in_progress,
                "message": self.server.scan_step_message,
                "cards_html": generate_cards_html(self.server.my_lat, self.server.my_lon)
            }
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        else:
            self.send_error(404)

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)

        if parsed_url.path == "/scan":
            if self.server.scan_in_progress:
                self.send_response(409)
                self.end_headers()
                self.wfile.write(b"SCAN_ALREADY_RUNNING")
                return

            self.server.my_lat, self.server.my_lon = get_android_gps()
            params = urllib.parse.parse_qs(parsed_url.query)
            radius_param = params.get('radius', [None])[0]
            if radius_param:
                try:
                    self.server.scan_radius = float(radius_param)
                except ValueError:
                    pass

            worker = threading.Thread(
                target=run_radar_scan_cycle,
                args=(self.server.my_lat, self.server.my_lon, self.server.scan_radius, self.server.pkmn_id, self.server)
            )
            worker.daemon = True
            worker.start()

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"STARTED")
            return

        elif parsed_url.path == "/caught":
            params = urllib.parse.parse_qs(parsed_url.query)
            enc_id = params.get('id', [None])[0]
            if enc_id:
                radar_cache = load_local_cache()
                if enc_id in radar_cache:
                    del radar_cache[enc_id]
                    save_local_cache(radar_cache)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")
                    return
            self.send_error(400)
        elif parsed_url.path == "/fetch-stats":
            # Execute synchronously. The browser will wait for this to finish before the 'fetch' resolves.
            success = fetch_and_populate_history(self.server.my_lat, self.server.my_lon)

            if success:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ENRICHED_SUCCESSFULLY")
            else:
                self.send_response(500)  # Send an error code if the History API failed
                self.end_headers()
                self.wfile.write(b"ENRICHMENT_FAILED")
            return


# --- LIVE GPS TRACKING ENGINE ---
def live_gps_updater(server_instance):
    """Background thread that continuously updates the server's location matrix."""
    while True:
        time.sleep(5)  # Poll every 5 seconds to balance responsiveness and battery life
        lat, lon = get_android_gps(silent=True)
        if lat and lon:
            server_instance.my_lat = lat
            server_instance.my_lon = lon


# --- RUN ORCHESTRATOR ---
def start_radar_workstation(pkmm_id, scan_radius):
    # Fetch initial coordinates
    my_lat, my_lon = get_android_gps()
    if not my_lat or not my_lon:
        print("[-] Absolute Failure: GPS tracking fix offline.")
        return

    # Perform one initial baseline map scan right out of the gate
    run_radar_scan_cycle(my_lat, my_lon, scan_radius, pkmn_id)

    print(f"\n📡 [ WEB DASHBOARD SERVER ONLINE ]")
    print(f"👉 Open your phone browser and go to: http://localhost:{PORT}")
    print("💡 Press CTRL+C inside Termux when you want to terminate the workstation environment.")

    # Establish network host bindings and handoff parameters
    server = HTTPServer(('127.0.0.1', PORT), RadarRequestHandler)
    server.my_lat = my_lat
    server.my_lon = my_lon
    server.scan_radius = scan_radius
    server.pkmn_id = pkmn_id

    # Global state management contexts
    server.scan_in_progress = False
    server.spawns_found_current = 0
    server.scan_step_message = ""

    # NEW: Start the background live GPS tracking thread
    gps_thread = threading.Thread(target=live_gps_updater, args=(server,))
    gps_thread.daemon = True
    gps_thread.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down system cluster panel. Good luck out there!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Portable Pokémon Go Radar Terminal Engine")

    # Add the radius parameter (defines the flag, type, default fallback, and help text)
    parser.add_argument(
        '-r', '--radius',
        type=float,
        default=5000.0,
        help="Scan radius in meters away from your phone's current position (Default: 5000)"
    )
    parser.add_argument(
        '-p', '--pokemon',
        type=str,
        default="Bulbasaur",
        help="Pokemon name (Case sensitive) (Default: Bulbasaur)"
    )

    # Parse the arguments supplied by the user in Termux
    args = parser.parse_args()
    pokemon = args.pokemon
    scan_radius = args.radius
    if pokemon not in POKEDEX_flip:
        print(f"{pokemon} does not exists! Default to Bulbasaur")
        pkmn_id = POKEDEX_flip["Bulbasaur"]
    else:
        pkmn_id = POKEDEX_flip[pokemon]
    print(f"Searching for {pokemon}({pkmn_id})")
    start_radar_workstation(pkmn_id, scan_radius)
