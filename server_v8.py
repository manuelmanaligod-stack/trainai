"""
TrainAI Server v8 — Railway Edition
-------------------------------------
Run locally: python server_v8.py
Deployed on: Railway
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json, requests, os
from groq import Groq
from datetime import datetime

STRAVA_CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID",     "234502")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "ce8eedc1d05808af8cfb2829c833abb5d69dc9f4")
STRAVA_REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN", "2ac675776dea85002172508cdeb748ec0fe1637c")
GROQ_API_KEY         = os.environ.get("GROQ_API_KEY",         "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")

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

def get_best_efforts(token):
    """Fetch best efforts from runs going back to 2025."""
    all_efforts = {}
    page = 1
    after_ts = int(datetime(2025, 1, 1).timestamp())

    while page <= 10:  # max 10 pages = 300 activities
        r = requests.get("https://www.strava.com/api/v3/athlete/activities",
                         headers={"Authorization": f"Bearer {token}"},
                         params={"per_page": 30, "page": page, "after": after_ts})
        if r.status_code != 200:
            break
        activities = r.json()
        if not activities:
            break

        runs = [a for a in activities if a.get("sport_type") == "Run"]
        for run in runs:
            detail = requests.get(
                f"https://www.strava.com/api/v3/activities/{run['id']}",
                headers={"Authorization": f"Bearer {token}"}
            )
            if detail.status_code != 200:
                continue
            data = detail.json()
            for effort in data.get("best_efforts", []):
                name = effort.get("name", "")
                elapsed = effort.get("elapsed_time", 0)
                date = effort.get("start_date_local", "")[:10]
                dist = effort.get("distance", 0)
                if name not in all_efforts or elapsed < all_efforts[name]["elapsed_time"]:
                    all_efforts[name] = {
                        "name": name,
                        "elapsed_time": elapsed,
                        "distance": dist,
                        "date": date,
                        "activity_name": run.get("name", ""),
                    }
        page += 1

    return list(all_efforts.values())

def classify_zone(avg_hr):
    if not avg_hr: return None
    bounds = [0,124,154,169,184,999]
    for i in range(5):
        if bounds[i] <= avg_hr < bounds[i+1]: return i+1
    return 5

def ask_groq_full(activities, goals=None):
    lines = []
    for i, a in enumerate(activities):
        sport    = a.get("sport_type","Unknown")
        distance = round(a.get("distance",0)/1000,2)
        duration = round(a.get("moving_time",0)/60,1)
        avg_hr   = a.get("average_heartrate","N/A")
        pace     = round(duration/distance,2) if distance>0 else "N/A"
        lines.append(f'{i}. [{a.get("start_date_local","")[:10]}] {sport} "{a.get("name","")}" | {distance}km {duration}min pace:{pace} HR:{avg_hr}')

    goals_text = ""
    if goals:
        goal_parts = []
        if goals.get("raceDate") and goals.get("raceDist"): goal_parts.append(f"Next race: {goals['raceDist']} on {goals['raceDate']} (goal: {goals.get('raceTime','')})")
        if goals.get("weeklyKm"): goal_parts.append(f"Weekly km goal: {goals['weeklyKm']}km")
        if goals.get("vo2Target"): goal_parts.append(f"VO2 max target: {goals['vo2Target']} (current: {goals.get('vo2Current','')})")
        if goals.get("pr5kGoal"): goal_parts.append(f"5K goal: {goals['pr5kGoal']} (current PR: {goals.get('pr5kCur','')})")
        if goals.get("pr10kGoal"): goal_parts.append(f"10K goal: {goals['pr10kGoal']} (current PR: {goals.get('pr10kCur','')})")
        if goals.get("pr21kGoal"): goal_parts.append(f"Half marathon goal: {goals['pr21kGoal']}")
        if goals.get("pr42kGoal"): goal_parts.append(f"Marathon goal: {goals['pr42kGoal']}")
        if goal_parts: goals_text = "\n\nAthlete's goals:\n" + "\n".join(goal_parts)

    client = Groq(api_key=GROQ_API_KEY)
    prompt = f"""Analyze these Strava workouts:{goals_text}

{chr(10).join(lines)}

HR Zones: Z1<124, Z2 124-154(Endurance), Z3 154-169(Tempo), Z4 169-184(Threshold), Z5 185+(Max)

Return ONLY valid JSON, no markdown, no extra text:
{{
  "workouts": [
    {{
      "index": 0,
      "highlight": "one sentence on what was done well — use 'you'",
      "description": "2 sentence description of this workout and training value — use 'you'",
      "comparison": "one sentence comparing to other workouts — use 'you'"
    }}
  ],
  "weekly_summary": "2-3 sentences on training pattern{' and progress toward goals' if goals_text else ''} — use 'you'",
  "analysis": "3-4 paragraph coaching analysis{' referencing goals' if goals_text else ''} — use 'you'",
  "next_workout": {{
    "type": "Run",
    "title": "short workout name",
    "description": "3-4 sentences on what to do next and why — use 'you'"
  }}
}}"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=3000
    )
    raw = response.choices[0].message.content
    raw = raw.replace("```json","").replace("```","").strip()
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
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length) if length else b'{}'
                try:
                    req_data = json.loads(body)
                    goals = req_data.get("goals", {})
                except:
                    goals = {}

                token      = get_access_token()
                activities = get_activities(token, 15)
                ai_data    = ask_groq_full(activities, goals)

                activity_list = []
                for i, a in enumerate(activities):
                    avg_hr  = a.get("average_heartrate")
                    buckets = []
                    if avg_hr:
                        buckets = get_activity_zones(token, a["id"])
                    wi = next((w for w in ai_data.get("workouts",[]) if w.get("index")==i), {})
                    activity_list.append({
                        "id":          a["id"],
                        "date":        a.get("start_date_local","")[:10],
                        "name":        a.get("name","Unknown"),
                        "sport":       a.get("sport_type","Unknown"),
                        "distance":    round(a.get("distance",0)/1000,2),
                        "duration":    round(a.get("moving_time",0)/60,1),
                        "avg_hr":      avg_hr,
                        "max_hr":      a.get("max_heartrate"),
                        "zone":        classify_zone(avg_hr),
                        "buckets":     buckets,
                        "highlight":   wi.get("highlight",""),
                        "description": wi.get("description",""),
                        "comparison":  wi.get("comparison",""),
                    })

                runs = [a for a in activities if a.get("sport_type")=="Run"]
                wts  = [a for a in activities if a.get("sport_type")=="WeightTraining"]

                # Fetch real best efforts from Strava
                try:
                    best_efforts = get_best_efforts(token)
                except:
                    best_efforts = []

                result = {
                    "success":        True,
                    "activities":     activity_list,
                    "weekly_summary": ai_data.get("weekly_summary",""),
                    "analysis":       ai_data.get("analysis",""),
                    "next_workout":   ai_data.get("next_workout",{}),
                    "best_efforts":   best_efforts,
                    "stats": {
                        "total_runs":      len(runs),
                        "total_km":        round(sum(a.get("distance",0) for a in runs)/1000,1),
                        "activities":      len(activities),
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
    port = int(os.environ.get("PORT", 8080))
    try: local_ip = socket.gethostbyname(socket.gethostname())
    except: local_ip = "localhost"
    print(f"\n🚴 TrainAI Server v8")
    print(f"─"*40)
    print(f"✅ Running on port {port}")
    print(f"📱 Local: http://{local_ip}:{port}")
    print(f"─"*40)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
