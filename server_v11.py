"""
TrainAI Server v11 - Clean Rebuild
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, requests, os, re
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

def get_all_activities(token):
    """Fetch all activities from Jan 2025 onwards."""
    all_acts, page = [], 1
    after_ts = int(datetime(2025, 1, 1).timestamp())
    while page <= 8:
        r = requests.get("https://www.strava.com/api/v3/athlete/activities",
                         headers={"Authorization": f"Bearer {token}"},
                         params={"per_page": 30, "page": page, "after": after_ts})
        if r.status_code != 200: break
        batch = r.json()
        if not batch: break
        all_acts.extend(batch)
        page += 1
    return all_acts

def get_activity_zones(token, activity_id):
    r = requests.get(f"https://www.strava.com/api/v3/activities/{activity_id}/zones",
                     headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 200:
        for z in r.json():
            if z.get("type") == "heartrate":
                return z.get("distribution_buckets", [])
    return []

def get_best_efforts(token):
    """Scan all runs from 2025 and collect best effort PRs."""
    all_efforts, page = {}, 1
    after_ts = int(datetime(2025, 1, 1).timestamp())
    while page <= 10:
        r = requests.get("https://www.strava.com/api/v3/athlete/activities",
                         headers={"Authorization": f"Bearer {token}"},
                         params={"per_page": 30, "page": page, "after": after_ts})
        if r.status_code != 200: break
        batch = r.json()
        if not batch: break
        runs = [a for a in batch if a.get("sport_type") == "Run"]
        for run in runs:
            detail = requests.get(f"https://www.strava.com/api/v3/activities/{run['id']}",
                                   headers={"Authorization": f"Bearer {token}"})
            if detail.status_code != 200: continue
            for effort in detail.json().get("best_efforts", []):
                name, elapsed = effort.get("name",""), effort.get("elapsed_time",0)
                date, dist = effort.get("start_date_local","")[:10], effort.get("distance",0)
                if name not in all_efforts or elapsed < all_efforts[name]["elapsed_time"]:
                    all_efforts[name] = {"name":name,"elapsed_time":elapsed,"distance":dist,"date":date,"activity":run.get("name","")}
        page += 1
    return list(all_efforts.values())

def classify_zone(avg_hr):
    if not avg_hr: return None
    for i, (lo,hi) in enumerate([(0,124),(124,154),(154,169),(169,184),(184,999)]):
        if lo <= avg_hr < hi: return i+1
    return 5

def ask_groq(activities, goals=None):
    lines = []
    for i, a in enumerate(activities[:15]):
        sport = a.get("sport_type","Unknown")
        dist  = round(a.get("distance",0)/1000, 2)
        dur   = round(a.get("moving_time",0)/60, 1)
        hr    = a.get("average_heartrate","N/A")
        pace  = round(dur/dist,2) if dist>0 else "N/A"
        lines.append(f'{i}. [{a.get("start_date_local","")[:10]}] {sport} "{a.get("name","")}" | {dist}km {dur}min pace:{pace} HR:{hr}')

    goals_txt = ""
    if goals:
        gp = []
        if goals.get("raceDate") and goals.get("raceDist"):
            gp.append(f"Next race: {goals['raceDist']} on {goals['raceDate']}{' goal: '+goals['raceTime'] if goals.get('raceTime') else ''}")
        if goals.get("weeklyKm"): gp.append(f"Weekly km goal: {goals['weeklyKm']}km")
        if goals.get("weeklyRuns"): gp.append(f"Weekly runs goal: {goals['weeklyRuns']}")
        if goals.get("pr5kGoal"): gp.append(f"5K goal: {goals['pr5kGoal']}")
        if goals.get("pr10kGoal"): gp.append(f"10K goal: {goals['pr10kGoal']}")
        if goals.get("pr21kGoal"): gp.append(f"Half goal: {goals['pr21kGoal']}")
        if goals.get("pr42kGoal"): gp.append(f"Marathon goal: {goals['pr42kGoal']}")
        if gp: goals_txt = "\n\nGoals:\n" + "\n".join(gp)

    client = Groq(api_key=GROQ_API_KEY)
    prompt = f"""Analyze these recent Strava workouts:{goals_txt}

{chr(10).join(lines)}

HR Zones: Z1<124(Recovery), Z2 124-154(Endurance), Z3 154-169(Tempo), Z4 169-184(Threshold), Z5 185+(Max)

Return ONLY valid JSON:
{{
  "workouts": [{{"index":0,"highlight":"what you did well (use 'you')","description":"2 sentences on this workout (use 'you')","comparison":"compare to others (use 'you')"}}],
  "summary": "3-4 sentences summarizing the last 15 workouts — volume, consistency, zone distribution (use 'you')",
  "analysis": "2-3 paragraphs of coaching analysis{' referencing goals' if goals_txt else ''} (use 'you')",
  "next_workout": {{"type":"Run","title":"workout name","description":"3-4 sentences on what to do next and why (use 'you')"}}
}}"""

    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role":"user","content":prompt}],
        max_tokens=4000
    )
    raw = resp.choices[0].message.content
    raw = raw.replace("```json","").replace("```","").strip()
    s, e = raw.find('{'), raw.rfind('}')+1
    if s >= 0 and e > s: raw = raw[s:e]
    try:
        return json.loads(raw)
    except:
        print(f"JSON error. Raw[:300]: {raw[:300]}")
        m = re.search(r'"summary"\s*:\s*"([^"]{10,})"', raw)
        return {
            "workouts": [],
            "summary": m.group(1) if m else "Tap Analyze again for full analysis.",
            "analysis": "Analysis unavailable — tap Analyze again.",
            "next_workout": {"type":"Run","title":"Easy Recovery Run","description":"Take it easy with a 30-40 min easy run at a comfortable conversational pace."}
        }

def read_file(path):
    with open(path) as f: return f.read()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]} {args[1]}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-type","text/html")
            self.end_headers()
            self.wfile.write(read_file("app_v11.html").encode())
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/analyze":
            try:
                length = int(self.headers.get("Content-Length",0))
                body = self.rfile.read(length) if length else b'{}'
                try: goals = json.loads(body).get("goals",{})
                except: goals = {}

                token = get_access_token()
                all_acts = get_all_activities(token)
                recent15 = all_acts[:15]

                # AI on recent 15
                ai = ask_groq(recent15, goals)

                # Build activity list (all, with AI insights on first 15)
                activity_list = []
                for i, a in enumerate(all_acts):
                    avg_hr = a.get("average_heartrate")
                    buckets = get_activity_zones(token, a["id"]) if avg_hr and i < 15 else []
                    wi = next((w for w in ai.get("workouts",[]) if w.get("index")==i), {})
                    activity_list.append({
                        "id":          a["id"],
                        "date":        a.get("start_date_local","")[:10],
                        "name":        a.get("name","Unknown"),
                        "sport":       a.get("sport_type","Unknown"),
                        "distance":    round(a.get("distance",0)/1000, 2),
                        "duration":    round(a.get("moving_time",0)/60, 1),
                        "avg_hr":      avg_hr,
                        "max_hr":      a.get("max_heartrate"),
                        "zone":        classify_zone(avg_hr),
                        "buckets":     buckets,
                        "highlight":   wi.get("highlight",""),
                        "description": wi.get("description",""),
                        "comparison":  wi.get("comparison",""),
                        "calories":    a.get("calories",0),
                    })

                # Best efforts
                try: best_efforts = get_best_efforts(token)
                except: best_efforts = []

                runs = [a for a in all_acts if a.get("sport_type")=="Run"]
                wts  = [a for a in all_acts if a.get("sport_type")=="WeightTraining"]

                result = {
                    "success": True,
                    "activities": activity_list,
                    "summary": ai.get("summary",""),
                    "analysis": ai.get("analysis",""),
                    "next_workout": ai.get("next_workout",{}),
                    "best_efforts": best_efforts,
                    "stats": {
                        "total_runs": len(runs),
                        "total_km": round(sum(a.get("distance",0) for a in runs)/1000, 1),
                        "total_activities": len(all_acts),
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
    port = int(os.environ.get("PORT", 8080))
    print(f"\n🚴 TrainAI Server v11")
    print(f"─"*40)
    print(f"✅ Running on port {port}")
    print(f"📱 http://{local_ip}:{port}")
    print(f"─"*40)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
