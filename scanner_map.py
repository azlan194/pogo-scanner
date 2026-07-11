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
scan_thread_active = False
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
    global scan_thread_active
    if server_instance:
        server_instance.scan_in_progress = True
        server_instance.spawns_found_current = 0
        server_instance.scan_step_message = "Initializing radar array..."

    print(f"\n📡 [API] Requesting fresh radar data frame (Radius: {int(radius)}m, Lat: {lat}, Lon: {lon})...")
    call_count = 1
    while scan_thread_active:  # <--- Changed from 'while True:'
        timestamp = datetime.datetime.now().strftime('%H:%M:%S')

        # 1. Pre-request feedback
        # print(f"[{timestamp}] 🔄 Loop #{call_count}: Sending discovery request to Coords API...")
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
        print(f"[{timestamp}] 🔄 Loop #{call_count}: Found pokemon spawned with distance: {dist}m")
        # print(f"[{timestamp}] 💤 Sleeping for {POLL_DELAY_SECONDS} seconds before starting Loop #{call_count + 1}...\n")
        time.sleep(POLL_DELAY_SECONDS)
        call_count += 1

    if server_instance:
        server_instance.scan_in_progress = False


# --- DYNAMIC HTML RENDER ENGINE ---
def generate_dashboard_html(current_lat, current_lon, current_radius):
    """Generates the main WebUI with an interactive Leaflet map, tracking, and controls."""

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>Mobile Radar</title>

        <!-- Leaflet CSS & JS -->
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

        <style>
            body {{ font-family: sans-serif; margin: 0; padding: 0; background: #121212; color: white; }}
            .header {{ padding: 15px; background: #1e1e1e; text-align: center; border-bottom: 2px solid #333; }}

            /* Controls Row & Buttons */
            .controls-row {{ display: flex; gap: 8px; padding: 10px; background: #1e1e1e; flex-wrap: wrap; }}
            .scan-banner, .radius-input {{ padding: 12px; font-size: 15px; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; }}
            .radius-input {{ width: 80px; background: #333; color: white; text-align: center; }}
            #globalScanBtn {{ background: #4CAF50; color: white; flex: 1; }}
            #fetchStatsBtn {{ background: #FF9800; color: white; flex: 1; }}

            /* Map & Floating Button Container */
            .map-container {{ position: relative; height: calc(100vh - 130px); width: 100%; }}
            #map {{ height: 100%; width: 100%; }}

            /* Floating Recenter Button */
            .recenter-btn {{
                position: absolute;
                bottom: 30px;
                right: 20px;
                z-index: 1000;
                background: #2196F3;
                color: white;
                border: none;
                border-radius: 50%;
                width: 55px;
                height: 55px;
                font-size: 24px;
                box-shadow: 0 4px 8px rgba(0,0,0,0.4);
                cursor: pointer;
                display: flex;
                justify-content: center;
                align-items: center;
            }}

            /* Dark mode popup styling */
            .leaflet-popup-content-wrapper {{ background: #2a2a2a; color: #fff; }}
            .leaflet-popup-tip {{ background: #2a2a2a; }}
            .popup-stats {{ color: #FFD54F; font-weight: bold; margin: 5px 0; }}
            .popup-btn {{ display: block; background: #2196F3; color: white; text-align: center; padding: 8px; border-radius: 5px; text-decoration: none; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h2 style="margin:0;">📡 Radar Active</h2>
        </div>

        <!-- RESTORED: Both Scan and Fetch buttons coexist -->
        <div class='controls-row'>
            <input type="number" id="radiusInput" class="radius-input" value="{int(current_radius)}" placeholder="Meters">
            <button id="globalScanBtn" onclick="triggerGlobalScan()">🛰️ Scan Spawns</button>
            <button id="fetchStatsBtn" onclick="triggerFetchStats()">📊 Fetch Stats</button>
            <label style="display:flex; align-items:center; background:#333; padding:5px 10px; border-radius:8px;">
                <input type="checkbox" id="manualMode" onchange="toggleManualMode()"> Manual Mode
            </label>
        </div>

        <div class="map-container">
            <div id="map"></div>
            <!-- NEW: Floating Recenter Button -->
            <button class="recenter-btn" onclick="snapToCar()" title="Recenter Map">🎯</button>
        </div>

        <script>
            // 1. Initialize map and variables
            let savedMode = localStorage.getItem('manualMode');
            let isManualMode = (savedMode === 'true');
            document.getElementById('manualMode').checked = isManualMode;
            
            let liveLat = {current_lat};
            let liveLon = {current_lon};
            
            // NEW: Restore Zoom level from memory, default to 15 if not found
            let savedZoom = localStorage.getItem('mapZoom');
            let currentZoom = savedZoom ? parseInt(savedZoom) : 15;
            
            // NEW: Override default coordinates if we are in Manual Mode and have a saved pin
            if (isManualMode) {{
                let savedLat = localStorage.getItem('manualLat');
                let savedLon = localStorage.getItem('manualLon');
                if (savedLat !== null && savedLon !== null) {{
                    liveLat = parseFloat(savedLat);
                    liveLon = parseFloat(savedLon);
                }}
            }}
            
            const map = L.map('map').setView([liveLat, liveLon], currentZoom);

            // NEW: Save zoom level to memory every time the user zooms in or out
            map.on('zoomend', function() {{
                localStorage.setItem('mapZoom', map.getZoom());
                currentZoom = map.getZoom();
            }});

            L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
                maxZoom: 19,
                attribution: '© OpenStreetMap'
            }}).addTo(map);

            // 2. Draw the transparent Scan Radius circle
            let scanRadius = L.circle([liveLat, liveLon], {{
                color: '#4CAF50',
                fillColor: '#4CAF50',
                fillOpacity: 0.1,
                weight: 2,
                radius: parseInt(document.getElementById('radiusInput').value) || 1000,
                interactive: false
            }}).addTo(map);
            
            // --- NEW: Dynamically resize the circle when the input changes ---
            document.getElementById('radiusInput').addEventListener('input', function(e) {{
                let newRadius = parseInt(e.target.value);
                // Only update if it's a valid number greater than 0
                if (!isNaN(newRadius) && newRadius > 0) {{
                    scanRadius.setRadius(newRadius);
                }}
            }});

            let gpsWatchId = null;

            // 3. Mark your current location (Draggable if Manual Mode was saved)
            let userMarker = L.marker([liveLat, liveLon], {{
                draggable: isManualMode, 
                icon: L.divIcon({{ html: '🔵', className: 'user-marker', iconSize: [20, 20] }})
            }}).addTo(map).bindPopup("<b>🚗 Your Location</b>");

            // --- NEW: Sync the circle while dragging and update server on drop ---
            userMarker.on('drag', function(e) {{
                if (isManualMode) {{
                    scanRadius.setLatLng(e.target.getLatLng());
                }}
            }});

            userMarker.on('dragend', function(e) {{
                if (isManualMode) {{
                    let pos = e.target.getLatLng();
                    scanRadius.setLatLng(pos);
                    liveLat = pos.lat;
                    liveLon = pos.lng;
                    
                    // NEW: Save the exact drop coordinates to memory
                    localStorage.setItem('manualLat', liveLat);
                    localStorage.setItem('manualLon', liveLon);
                    
                    fetch('/update-location', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ lat: pos.lat, lon: pos.lng }})
                    }});
                }}
            }});
            
            map.on('click', function(e) {{
                if (isManualMode) {{
                    let pos = e.latlng;
                    liveLat = pos.lat;
                    liveLon = pos.lng;
                    
                    userMarker.setLatLng(pos);
                    scanRadius.setLatLng(pos);
                    
                    // NEW: Save the exact tap coordinates to memory
                    localStorage.setItem('manualLat', liveLat);
                    localStorage.setItem('manualLon', liveLon);
                    
                    fetch('/update-location', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ lat: pos.lat, lon: pos.lng }})
                    }});
                }}
            }});

            // 4. Toggle Mode Logic
            function toggleManualMode() {{
                isManualMode = document.getElementById('manualMode').checked;
                localStorage.setItem('manualMode', isManualMode); // Save to browser memory
                
                if (isManualMode) {{
                    if (gpsWatchId) navigator.geolocation.clearWatch(gpsWatchId);
                    userMarker.dragging.enable();
                    alert("Manual Mode Enabled: Drag the blue dot to scan a new area.");
                }} else {{
                    userMarker.dragging.disable();
                    startGpsTracking();
                }}
            }}

            // 5. GPS Tracking stream
            function startGpsTracking() {{
                if ('geolocation' in navigator) {{
                    gpsWatchId = navigator.geolocation.watchPosition(
                        (position) => {{
                            if (!isManualMode) {{
                                liveLat = position.coords.latitude;
                                liveLon = position.coords.longitude;
                                
                                let newLatLng = new L.LatLng(liveLat, liveLon);
                                userMarker.setLatLng(newLatLng);
                                scanRadius.setLatLng(newLatLng);
                                
                                // Keep Python server updated as you drive
                                fetch('/update-location', {{
                                    method: 'POST',
                                    headers: {{ 'Content-Type': 'application/json' }},
                                    body: JSON.stringify({{ lat: liveLat, lon: liveLon }})
                                }});
                            }}
                        }},
                        (err) => console.warn('GPS error:', err),
                        {{ enableHighAccuracy: true, maximumAge: 0, timeout: 5000 }}
                    );
                }}
            }}

            // Start tracking on load ONLY if not in manual mode
            if (!isManualMode) {{
                startGpsTracking();
            }}

            // 6. Snap to Car
            function snapToCar() {{
                map.setView([liveLat, liveLon], currentZoom, {{ animate: true }});
            }}

            // 7. Fetch target data and plot Sprites
            fetch('/api/spawns')
                .then(res => res.json())
                .then(data => {{
                    data.forEach(spawn => {{
                        let statsHtml = spawn.cp 
                            ? `<div class="popup-stats">⭐ $100% IV (${{spawn.stats}})<br>CP ${{spawn.cp}}<br>Level ${{spawn.lvl}}</div>`
                            : `<div style="color:#888; font-style:italic;">📊 Stats Unscanned</div>`;
                        
                        let driveInfo = spawn.drive_text ? `🚗 ${{spawn.drive_text}} drive` : 'Drive time unknown';
                        
                        let popupContent = `
                            <h3 style="margin:0 0 5px 0; text-align:center;">${{spawn.name}}</h3>
                            ${{statsHtml}}
                            <div>${{driveInfo}}</div>
                            <div>⏳ Despawns: ${{spawn.despawn_time}}</div>
                            <a href="${{spawn.maps_url}}" class="popup-btn" target="_blank">🗺️ Navigate</a>
                        `;

                        let spriteUrl = `https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/${{spawn.pokemon_id}}.png`;
                        
                        let pkmnIcon = L.icon({{
                            iconUrl: spriteUrl,
                            iconSize: [64, 64],
                            iconAnchor: [32, 32],
                            popupAnchor: [0, -32],
                            className: 'pkmn-marker'
                        }});

                        L.marker([spawn.latitude, spawn.longitude], {{icon: pkmnIcon}})
                            .addTo(map)
                            .bindPopup(popupContent);
                    }});
                }})
                .catch(err => console.error("Error loading spawn data:", err));

            // 8. Button Triggers
            function triggerGlobalScan() {{
                const radius = document.getElementById('radiusInput').value;
                
                // Explicitly send the live map coordinates to the Python server
                const scanUrl = `/scan?lat=${{liveLat}}&lon=${{liveLon}}&radius=${{radius}}`;
                
                fetch(scanUrl, {{ method: 'POST' }})
                .then(() => alert(`Discovery Phase started at ${{liveLat.toFixed(4)}}, ${{liveLon.toFixed(4)}}`))
                .catch(() => alert('Network error.'));
            }}
            

            function triggerFetchStats() {{
                const btn = document.getElementById('fetchStatsBtn');
                btn.disabled = true;
                btn.innerText = '⏳ Pulling Maps...';

                fetch('/fetch-stats', {{ method: 'POST' }})
                .then(res => {{
                    if (res.ok) window.location.reload();
                    else {{ alert('Error fetching stats.'); btn.disabled = false; btn.innerText = '📊 Fetch Stats'; }}
                }})
                .catch(() => {{ alert('Network error.'); btn.disabled = false; btn.innerText = '📊 Fetch Stats'; }});
            }}
        </script>
    </body>
    </html>
    """
    return html


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
                "message": self.server.scan_step_message
                # "cards_html": generate_cards_html(self.server.my_lat, self.server.my_lon)
            }
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        # --- NEW: JSON API Endpoint for the Map ---
        elif parsed_url.path == "/api/spawns":
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()

            radar_cache = load_local_cache()
            radar_cache = clean_expired_entries(radar_cache)

            # Convert the dictionary to a flat list for JavaScript
            spawns_list = list(radar_cache.values())
            self.wfile.write(json.dumps(spawns_list).encode('utf-8'))
            return
        else:
            self.send_error(404)

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)

        if parsed_url.path == "/scan":
            query_components = urllib.parse.parse_qs(parsed_url.query)

            # 1. Pull the exact map coordinates sent by the browser
            target_lat = float(query_components.get('lat', [self.server.my_lat])[0])
            target_lon = float(query_components.get('lon', [self.server.my_lon])[0])
            radius = int(query_components.get('radius', [1000])[0])

            # 2. Update the global server state so Fetch Stats uses the new location too
            self.server.my_lat = target_lat
            self.server.my_lon = target_lon

            # 3. Safely handle the background loop
            global scan_thread_active
            scan_thread_active = False  # Kill any existing background scanner
            time.sleep(0.5)  # Give it a moment to die
            scan_thread_active = True  # Green light for the new scanner

            # 4. Start the new scanner thread with the new coordinates
            worker = threading.Thread(
                target=run_radar_scan_cycle,
                args=(target_lat, target_lon, radius, self.server.pkmn_id)
            )
            worker.daemon = True
            worker.start()

            self.send_response(200)
            self.end_headers()
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

        # Add this route to do_POST in RadarRequestHandler
        elif parsed_url.path == "/update-location":
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))

            # Update the global server coordinates
            self.server.my_lat = float(data['lat'])
            self.server.my_lon = float(data['lon'])

            self.send_response(200)
            self.end_headers()
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
