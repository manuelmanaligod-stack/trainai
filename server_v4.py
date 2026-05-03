"""
TrainAI Server v4 - Full HR Zone Breakdown Per Activity
--------------------------------------------------------
Run with: python3 server_v4.py
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json, requests
from groq import Groq
from datetime import datetime

STRAVA_CLIENT_ID     = "234502"
STRAVA_CLIENT_SECRET = "ce8eedc1d05808af8cfb2829c833abb5d69dc9f4"
STRAVA_REFRESH_TOKEN = "2ac675776dea85002172508cdeb748ec0fe1637c"
GROQ_API_KEY         = "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"

def get_access_token():
    r = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": STRAVA_CLIENT_ID, "client_secret": STRAVA_CLIENT_SECRET,
        "refresh_token": STRAVA_REFRESH_TOKEN, "grant_type": "refresh_token"
    })
    r.raise_for_status()
    return r.json()["access_token"]

def get_athlete_zones(token):
    r = requests.get("https://www.strava.com/api/v3/athlete/zones",
                     headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 200:
        return r.json().get("heart_rate", {}).get("zones", [])
    return []

def get_activities(token, count=15):
    r = requests.get("https://www.strava.com/api/v3/athlete/activities",
                     headers={"Authorization": f"Bearer {token}"},
                     params={"per_page": count})
    r.raise_for_status()
    return r.json()

def get_activity_zones(token, activity_id):
    r = requests.get(f"https://www.strava.com/api/v3/activities/{activity_id}/zones",
                     headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 200:
        for z in r.json():
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

def ask_groq(activities, hr_zones):
    zone_names = ["Z1 Recovery","Z2 Endurance","Z3 Tempo","Z4 Threshold","Z5 Max"]
    lines = []
    for a in activities:
        sport    = a.get("sport_type","Unknown")
        distance = round(a.get("distance",0)/1000,2)
        duration = round(a.get("moving_time",0)/60,1)
        avg_hr   = a.get("average_heartrate","N/A")
        lines.append(f"- [{a.get('start_date_local','')[:10]}] {sport}: \"{a.get('name','')}\" | {distance}km {duration}min | HR:{avg_hr}")

    zone_info = ""
    if hr_zones:
        zone_info = "\nMy HR Zones: " + ", ".join(
            f"{zone_names[i] if i<len(zone_names) else f'Z{i+1}'}: {z.get('min')}-{z.get('max')}bpm"
            for i,z in enumerate(hr_zones)
        )

    client = Groq(api_key=GROQ_API_KEY)
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role":"user","content":f"""Recent Strava activities:
{chr(10).join(lines)}
{zone_info}

1. Briefly summarize training volume and consistency
2. Analyze HR zone distribution - am I training too hard or too easy?
3. Give 2-3 specific recommendations

Be concise and encouraging."""}],
        max_tokens=800
    )
    return response.choices[0].message.content

def read_file(path):
    with open(path,"r") as f: return f.read()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]} {args[1]}")

    def do_GET(self):
        if self.path in ("/","/index.html"):
            self.send_response(200)
            self.send_header("Content-type","text/html")
            self.end_headers()
            self.wfile.write(read_file("app_v7.html").encode())
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/analyze":
            try:
                token      = get_access_token()
                activities = get_activities(token, 15)
                hr_zones   = get_athlete_zones(token)

                activity_list = []
                for a in activities:
                    sport  = a.get("sport_type","Unknown")
                    avg_hr = a.get("average_heartrate")
                    buckets = []
                    if avg_hr:  # only fetch zones if HR data exists
                        buckets = get_activity_zones(token, a["id"])

                    activity_list.append({
                        "id":       a["id"],
                        "date":     a.get("start_date_local","")[:10],
                        "name":     a.get("name","Unknown"),
                        "sport":    sport,
                        "distance": round(a.get("distance",0)/1000,2),
                        "duration": round(a.get("moving_time",0)/60,1),
                        "avg_hr":   avg_hr,
                        "max_hr":   a.get("max_heartrate"),
                        "zone":     classify_zone(avg_hr, hr_zones),
                        "buckets":  buckets,
                    })

                runs  = [a for a in activities if a.get("sport_type")=="Run"]
                walks = [a for a in activities if a.get("sport_type")=="Walk"]
                wts   = [a for a in activities if a.get("sport_type")=="WeightTraining"]

                result = {
                    "success":    True,
                    "analysis":   ask_groq(activities, hr_zones),
                    "hr_zones":   hr_zones,
                    "activities": activity_list,
                    "stats": {
                        "total_runs": len(runs),
                        "total_km":   round(sum(a.get("distance",0) for a in runs)/1000,1),
                        "activities": len(activities),
                        "weight_sessions": len(wts),
                    }
                }
            except Exception as e:
                import traceback
                result = {"success":False,"error":str(e),"trace":traceback.format_exc()}

            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-type","application/json")
            self.send_header("Content-Length",len(body))
            self.send_header("Access-Control-Allow-Origin","*")
            self.end_headers()
            self.wfile.write(body)

if __name__ == "__main__":
    import socket
    try: local_ip = socket.gethostbyname(socket.gethostname())
    except: local_ip = "localhost"
    port = 8080
    print(f"\n🚴 TrainAI Server v4")
    print(f"─"*40)
    print(f"✅ Server running!")
    print(f"📱 Phone: http://{local_ip}:{port}")
    print(f"💻 Mac:   http://localhost:{port}")
    print(f"─"*40)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
