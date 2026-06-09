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
POLL_DELAY_SECONDS = 10
API_URL = "https://api2.flymego.io/Coords/WildEncounter"
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
FLYMEGO_TOKEN = os.getenv("FLYMEGO_AUTH_TOKEN")

with open('pokemon_id_map.json') as fi:
    poke_map = json.load(fi)['pokemon']
    POKEDEX = {int(k): v['name'] for k, v in poke_map.items()}
    POKEDEX_flip = {name: id for id, name in POKEDEX.items()}

HEADERS = {
    "authorization": FLYMEGO_TOKEN if FLYMEGO_TOKEN else "Bearer MISSING_TOKEN",
    "content-type": "application/x-protobuf",
    "accept-encoding": "gzip",
    "user-agent": "okhttp/4.12.0"
}

TEMPLATE_BYTES = b'\x0a$49edd87a-3ff3-49fd-b674-5ced81c0fafe\x12\x15\x18\x01 -(-0\x01=\xd4}\xc0=EYQ;?\xf0\x01\x01 \x02*\x1b\t`\x1f\x9d\xba\xf2\xd1B@\x11p_\x07\xce\x19\x9b^\xc0\x19\x00\x00\x00\x00\x00\x88\xc3@2\x18\t\xd8\xd4yT\xfc\xb9B@\x11\xb5\xcbm\xb1\x11{^\xc0\x18\x8b\xb6\xee\xfc\x11A\xa9\x93"\xe8\x1etC@I\x00\x08\x0e\x1e\\BS\xc0'


# --- NATIVE ANDROID GPS EXTRACTION ---
def get_android_gps():
    """Queries the Termux:API binary for real-time hardware GPS coordinates."""
    print("[*] Requesting hardware GPS fix from Android...")
    try:
        # Run the system command 'termux-location' with a 5-second timeout
        # using 'gps' provider for highest accuracy, 'last' can be used for instant cache
        result = subprocess.run(
            ['termux-location', '-p', 'gps'],
            capture_output=True,
            text=True,
            timeout=8
        )

        if result.returncode == 0:
            location_data = json.loads(result.stdout)
            lat = location_data.get('latitude')
            lon = location_data.get('longitude')
            if lat and lon:
                print(f"[✔] Location Acquired: {lat}, {lon} (Accuracy: {location_data.get('accuracy')}m)")
                return lat, lon
        print("[-] Termux:API returned an empty fix. Ensure GPS/Location is toggled ON.")
    except subprocess.TimeoutExpired:
        print("[-] GPS request timed out. Trying to fallback to last known network location...")
        try:
            result = subprocess.run(['termux-location', '-p', 'network'], capture_output=True, text=True, timeout=3)
            location_data = json.loads(result.stdout)
            return location_data.get('latitude'), location_data.get('longitude')
        except Exception:
            pass
    except Exception as e:
        print(f"[-] Execution error talking to Termux:API framework: {e}")
        print("[-] Ensure you installed the 'Termux:API' app from your App Store.")

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
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    cleaned_cache = {k: v for k, v in cache_dict.items() if v.get("despawn_ms", 0) > now_ms}
    expired_count = len(cache_dict) - len(cleaned_cache)
    if expired_count > 0:
        print(f"[*] Cleaned {expired_count} expired entries out of local cache.")
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
    url = "https://api2.flymego.io/Coords/WildEncounter"
    try:
        response = requests.post(url, headers=HEADERS, data=payload_bytes, timeout=10)
        if response.status_code == 200:
            return response.content
        print(f"[-] HTTP Error {response.status_code}")
        return None
    except Exception as e:
        print(f"[-] Network error: {e}")
        return None


def process_radar_feed(raw_response_bytes, current_lat, current_lon, pkmn_id):
    """Processes the server data payload. Returns True if a new spawn was caught, False otherwise."""
    radar_cache = load_local_cache()
    radar_cache = clean_expired_entries(radar_cache)

    if not raw_response_bytes:
        return False, 0

    message, typedef = blackboxprotobuf.decode_message(raw_response_bytes)

    # TERMINATION TRIGGER: Server key '2' missing or empty means queue is exhausted
    if '2' not in message or not message['2']:
        return False, 0

    if '2' in typedef and 'message_typedef' in typedef['2']:
        typedef['2']['message_typedef']['2']['type'] = 'double'
        typedef['2']['message_typedef']['3']['type'] = 'double'

    clean_message, _ = blackboxprotobuf.decode_message(raw_response_bytes, typedef)
    encounters = clean_message.get('2', [])
    if isinstance(encounters, dict):
        encounters = [encounters]

    batch_count = len(encounters)
    new_data_added = batch_count > 0

    for spawn in encounters:
        enc_id = spawn.get('1', b'unknown_id')
        if isinstance(enc_id, bytes):
            enc_id = enc_id.decode('utf-8', errors='ignore')

        lat = spawn.get('2', 0.0)
        lon = spawn.get('3', 0.0)
        despawn_ms = spawn.get('12', 0)
        time_str = "Unknown"
        if despawn_ms:
            dt_obj = datetime.datetime.fromtimestamp(despawn_ms / 1000.0, tz=ZoneInfo("America/Los_Angeles"))
            time_str = dt_obj.strftime('%I:%M:%S %p PDT')
        dist = calculate_distance(current_lat, current_lon, lat, lon)

        # Call Google Maps API to fetch real-time drive metrics
        drive_seconds, drive_text = get_driving_info(current_lat, current_lon, lat, lon)

        radar_cache[enc_id] = {
            "encounter_id": enc_id,
            "pokemon_id": pkmn_id,
            "name": POKEDEX.get(pkmn_id, f"Unknown ({pkmn_id})"),
            "latitude": lat,
            "longitude": lon,
            "distance_meters": dist,
            "despawn_time": time_str,
            "maps_url": f"https://www.google.com/maps/search/?api=1&query={lat},{lon}",
            "despawn_ms": despawn_ms,
            "drive_seconds": drive_seconds,
            "drive_text": drive_text
        }
        print(f"[+] Found & cached target: {radar_cache[enc_id]['name']} ({dist}m away)")
        new_data_added = True

    sorted_list = sorted(radar_cache.values(), key=lambda x: x['distance_meters'])
    sorted_dict = {l["encounter_id"]: l for l in sorted_list}
    save_local_cache(sorted_dict)
    return new_data_added, batch_count


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
        if server_instance:
            server_instance.scan_step_message = f"Querying batch #{call_count}..."

        payload = create_payload(lat, lon, radius)
        response_data = fetch_spawns(payload)
        has_more_data, batch_count = process_radar_feed(response_data, lat, lon, pkmn_id)

        if server_instance:
            server_instance.spawns_found_current += batch_count
            server_instance.scan_step_message = f"Batch #{call_count}: Found {server_instance.spawns_found_current} targets..."

        if not has_more_data:
            print("[✔] Map space update complete.")
            break
        time.sleep(POLL_DELAY_SECONDS)
        call_count += 1

    if server_instance:
        server_instance.scan_in_progress = False


# --- DYNAMIC HTML RENDER ENGINE ---
def generate_cards_html(current_lat, current_lon):
    """Generates strictly the HTML for the target cards. Used by both full page load and AJAX updates."""
    radar_cache = load_local_cache()
    radar_cache = clean_expired_entries(radar_cache)
    sorted_list = sorted(radar_cache.values(), key=lambda x: x['distance_meters']) if radar_cache else []

    html = [f"<h2>🟢 Active Mobile Targets ({len(sorted_list)})</h2>"]

    if not sorted_list:
        html.append("<p style='color:#777;'>🏜️ No targets found within range. Click the Scan banner above!</p>")

    for index, target in enumerate(sorted_list, 1):
        now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
        seconds_left = max(0, int((target['despawn_ms'] - now_ms) / 1000))

        drive_seconds, drive_text = get_driving_info(current_lat, current_lon, target['latitude'], target['longitude'])
        is_catchable = drive_seconds is not None and seconds_left > (drive_seconds + 60)

        border_class = "green" if is_catchable else "red"
        html_status = f"🚗 Catchable! ({drive_text} drive)" if is_catchable else f"⚠️ Too Late! ({drive_text} drive)"
        # stats_str = f"100% IV ({target['atk']}/{target['dfn']}/{target['sta']}) - Lvl {target['level']}" if target['atk'] is not None else "Unscanned tier"

        maps_url = f"https://www.google.com/maps/search/?api=1&query={target['latitude']},{target['longitude']}"
        card_dom_id = f"card_{target['encounter_id']}"

        html.append(f"""
        <div class="card {border_class}" id="{card_dom_id}">
            <div class="name">#{index}. {target['name']} ({target['distance_meters']}m away)</div>
            <div><b>Status:</b> {html_status}</div>
            <div><b>Time Left:</b> <span class="live-timer" id="timer_{index}" data-card-id="{card_dom_id}" data-time="{target['despawn_ms']}">Calculating...</span></div>
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
        "  <button id='globalScanBtn' class='scan-banner' onclick='triggerGlobalScan()'>🛰️ Scan for New Spawns</button>",
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
