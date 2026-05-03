"""
TrainAI Server v8
------------------
Run with: python3 server_v8.py
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

def classify_zone(avg_hr):
    if not avg_hr: return None
    bounds = [0,124,154,169,184,999]
    for i in range(5):
        if bounds[i] <= avg_hr < bounds[i+1]: return i+1
    return 5

def ask_groq_full(activities):
    """Returns JSON with per-workout insights + weekly summary + next workout rec."""
    lines = []
    for i, a in enumerate(activities):
        sport    = a.get("sport_type","Unknown")
        distance = round(a.get("distance",0)/1000,2)
        duration = round(a.get("moving_time",0)/60,1)
        avg_hr   = a.get("average_heartrate","N/A")
        pace     = round(duration/distance,2) if distance>0 else "N/A"
        lines.append(f'{i}. [{a.get("start_date_local","")[:10]}] {sport} "{a.get("name","")}" | {distance}km {duration}min pace:{pace} HR:{avg_hr}')

    client = Groq(api_key=GROQ_API_KEY)
    prompt = f"""Analyze these Strava workouts:

{chr(10).join(lines)}

HR Zones: Z1<124, Z2 124-154(Endurance), Z3 154-169(Tempo), Z4 169-184(Threshold), Z5 185+(Max)

Return ONLY valid JSON, no markdown, no explanation:
{{
  "workouts": [
    {{
      "index": 0,
      "highlight": "one sentence on what the athlete did well in this specific workout — never use any names, use 'you' instead",
      "description": "2 sentence description of this workout and its training value — use 'you' not any names",
      "comparison": "one sentence comparing this workout to others in the list — use 'you' not any names"
    }}
  ],
  "weekly_summary": "2-3 sentence summary of the week's training pattern and consistency — use 'you' not any names",
  "next_workout": {{
    "type": "Run/Walk/WeightTraining",
    "title": "short workout name",
    "description": "3-4 sentence detailed recommendation for the next workout based on what has been done this week. Use 'you'. Include specific duration, intensity, and why."
  }}
}}"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=3000
    )
    raw = response.choices[0].message.content
    raw = raw.replace("```json","").replace("```","").strip()
    # Try to extract JSON if there's extra text around it
    start = raw.find('{')
    end   = raw.rfind('}') + 1
    if start >= 0 and end > start:
        raw = raw[start:end]
    return json.loads(raw)

def read_file(path):
    with open(path,"r") as f: return f.read()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]} {args[1]}")

    def do_GET(self):
        if self.path in ("/","/index.html"):
            self.send_response(200)
            self.send_header("Content-type","text/html")
            self.end_headers()
            self.wfile.write(read_file("app_v8.html").encode())
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/analyze":
            try:
                token      = get_access_token()
                activities = get_activities(token, 15)
                ai_data    = ask_groq_full(activities)

                activity_list = []
                for i, a in enumerate(activities):
                    avg_hr  = a.get("average_heartrate")
                    buckets = []
                    if avg_hr:
                        buckets = get_activity_zones(token, a["id"])

                    wi = next((w for w in ai_data.get("workouts",[]) if w.get("index")==i), {})

                    activity_list.append({
                        "id":         a["id"],
                        "date":       a.get("start_date_local","")[:10],
                        "name":       a.get("name","Unknown"),
                        "sport":      a.get("sport_type","Unknown"),
                        "distance":   round(a.get("distance",0)/1000,2),
                        "duration":   round(a.get("moving_time",0)/60,1),
                        "avg_hr":     avg_hr,
                        "max_hr":     a.get("max_heartrate"),
                        "zone":       classify_zone(avg_hr),
                        "buckets":    buckets,
                        "highlight":  wi.get("highlight",""),
                        "description": wi.get("description",""),
                        "comparison": wi.get("comparison",""),
                    })

                runs = [a for a in activities if a.get("sport_type")=="Run"]
                wts  = [a for a in activities if a.get("sport_type")=="WeightTraining"]

                result = {
                    "success":        True,
                    "activities":     activity_list,
                    "weekly_summary": ai_data.get("weekly_summary",""),
                    "next_workout":   ai_data.get("next_workout",{}),
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
    print(f"\n🚴 TrainAI Server v8")
    print(f"─"*40)
    print(f"✅ Server running!")
    print(f"📱 Phone: http://{local_ip}:{port}")
    print(f"💻 Mac:   http://localhost:{port}")
    print(f"─"*40)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
