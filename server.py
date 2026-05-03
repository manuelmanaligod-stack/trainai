"""
Strava Training Analyzer - Web Server with per-activity HR Zones
-----------------------------------------------------------------
Run with: python3 server.py
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import requests
from groq import Groq
from datetime import datetime

# ─── YOUR CREDENTIALS ───────────────────────────────────────────────────────

STRAVA_CLIENT_ID     = "234502"
STRAVA_CLIENT_SECRET = "ce8eedc1d05808af8cfb2829c833abb5d69dc9f4"
STRAVA_REFRESH_TOKEN = "2ac675776dea85002172508cdeb748ec0fe1637c"
GROQ_API_KEY         = "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"

# ────────────────────────────────────────────────────────────────────────────

def get_access_token():
    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id":     STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "refresh_token": STRAVA_REFRESH_TOKEN,
            "grant_type":    "refresh_token",
        }
    )
    response.raise_for_status()
    return response.json()["access_token"]

def get_athlete_zones(access_token):
    response = requests.get(
        "https://www.strava.com/api/v3/athlete/zones",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    if response.status_code == 200:
        return response.json().get("heart_rate", {}).get("zones", [])
    return []

def get_activities(access_token, count=15):
    response = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"per_page": count}
    )
    response.raise_for_status()
    return response.json()

def get_activity_zones(access_token, activity_id):
    """Fetch HR zone breakdown for any activity."""
    response = requests.get(
        f"https://www.strava.com/api/v3/activities/{activity_id}/zones",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    if response.status_code == 200:
        for z in response.json():
            if z.get("type") == "heartrate":
                return z.get("distribution_buckets", [])
    return []

def classify_zone(avg_hr, zones):
    if not zones or not avg_hr:
        return None
    for i, z in enumerate(zones):
        if z.get("min", 0) <= avg_hr < z.get("max", 9999):
            return i + 1
    return len(zones)

def format_activities_for_ai(activities, hr_zones):
    zone_names = ["Z1 Recovery", "Z2 Endurance", "Z3 Tempo", "Z4 Threshold", "Z5 Max"]
    lines = []
    for a in activities:
        date     = a.get("start_date_local", "")[:10]
        sport    = a.get("sport_type", "Unknown")
        distance = round(a.get("distance", 0) / 1000, 2)
        duration = round(a.get("moving_time", 0) / 60, 1)
        avg_hr   = a.get("average_heartrate", "N/A")
        avg_pace = (
            round(a.get("moving_time", 0) / 60 / (a.get("distance", 1) / 1000), 2)
            if a.get("distance") else "N/A"
        )
        lines.append(
            f"- [{date}] {sport}: \"{a.get('name','Unknown')}\" | "
            f"{distance} km in {duration} min | Pace: {avg_pace} min/km | "
            f"Avg HR: {avg_hr}"
        )

    zone_info = ""
    if hr_zones:
        zone_info = "\n\nMy Strava HR Zones:\n"
        for i, z in enumerate(hr_zones):
            name = zone_names[i] if i < len(zone_names) else f"Zone {i+1}"
            zone_info += f"- {name}: {z.get('min')}–{z.get('max')} bpm\n"

    return "\n".join(lines) + zone_info

def ask_groq(activities_text):
    client = Groq(api_key=GROQ_API_KEY)
    prompt = f"""Here are my recent Strava activities with my HR zones:

{activities_text}

Please:
1. Summarize my recent training (volume, consistency, sports mix)
2. Analyze my heart rate patterns — which zones am I spending most time in across activities?
3. Give me 2-3 specific, actionable training recommendations

Be encouraging but specific. Keep it concise.
"""
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000
    )
    return response.choices[0].message.content

def read_file(path):
    with open(path, "r") as f:
        return f.read()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]} {args[1]}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(read_file("index.html").encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/analyze":
            try:
                token      = get_access_token()
                activities = get_activities(token, 15)
                hr_zones   = get_athlete_zones(token)

                activity_list = []
                zone_totals   = [0, 0, 0, 0, 0]

                for a in activities:
                    sport    = a.get("sport_type", "Unknown")
                    avg_hr   = a.get("average_heartrate")
                    zone_num = classify_zone(avg_hr, hr_zones) if avg_hr else None

                    # Fetch per-activity zone buckets for ALL activities with HR
                    buckets = []
                    if avg_hr:
                        buckets = get_activity_zones(token, a["id"])
                        for i, b in enumerate(buckets[:5]):
                            zone_totals[i] += b.get("time", 0)

                    activity_list.append({
                        "id":       a["id"],
                        "date":     a.get("start_date_local", "")[:10],
                        "name":     a.get("name", "Unknown"),
                        "sport":    sport,
                        "distance": round(a.get("distance", 0) / 1000, 2),
                        "duration": round(a.get("moving_time", 0) / 60, 1),
                        "avg_hr":   avg_hr,
                        "max_hr":   a.get("max_heartrate"),
                        "zone":     zone_num,
                        "buckets":  buckets,
                    })

                text     = format_activities_for_ai(activities, hr_zones)
                analysis = ask_groq(text)

                runs  = [a for a in activities if a.get("sport_type") == "Run"]
                walks = [a for a in activities if a.get("sport_type") == "Walk"]
                wts   = [a for a in activities if a.get("sport_type") == "WeightTraining"]

                result = {
                    "success":     True,
                    "analysis":    analysis,
                    "hr_zones":    hr_zones,
                    "zone_totals": zone_totals,
                    "activities":  activity_list,
                    "stats": {
                        "total_runs":      len(runs),
                        "total_km":        round(sum(a.get("distance",0) for a in runs) / 1000, 1),
                        "activities":      len(activities),
                        "weight_sessions": len(wts),
                    }
                }
            except Exception as e:
                import traceback
                result = {"success": False, "error": str(e), "trace": traceback.format_exc()}

            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Content-Length", len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

if __name__ == "__main__":
    import socket
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except:
        local_ip = "localhost"
    port = 8080
    print(f"\n🚴 Strava Training Analyzer")
    print(f"─" * 40)
    print(f"✅ Server running!")
    print(f"📱 Open on your phone: http://{local_ip}:{port}")
    print(f"💻 Or on this Mac:     http://localhost:{port}")
    print(f"─" * 40)
    print(f"Press Ctrl+C to stop\n")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
