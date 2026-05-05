"""
TrainAI Server v13 — Gemini Flash AI
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, requests, os, re
from datetime import datetime

STRAVA_CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID",     "234502")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "ce8eedc1d05808af8cfb2829c833abb5d69dc9f4")
STRAVA_REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN", "2ac675776dea85002172508cdeb748ec0fe1637c")
GEMINI_API_KEY       = os.environ.get("GEMINI_API_KEY",       "AIzaSyA85SNrCK7mrWMJQSX9fq5XVfsV8fKnGGs")

def get_access_token():
    r = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": STRAVA_CLIENT_ID, "client_secret": STRAVA_CLIENT_SECRET,
        "refresh_token": STRAVA_REFRESH_TOKEN, "grant_type": "refresh_token"
    })
    r.raise_for_status()
    return r.json()["access_token"]

def get_all_activities(token):
    """Fetch activities newest first — no after filter so page 1 is always most recent."""
    all_acts = []
    for page in range(1, 6):
        r = requests.get("https://www.strava.com/api/v3/athlete/activities",
                         headers={"Authorization": f"Bearer {token}"},
                         params={"per_page": 30, "page": page})
        if r.status_code != 200: break
        batch = r.json()
        if not batch: break
        all_acts.extend(batch)
        # Stop once we pass Jan 2025
        oldest = batch[-1].get("start_date", "9999")
        if oldest < "2025-01-01T00:00:00Z": break
    # Sort newest first, filter to 2025+
    all_acts.sort(key=lambda a: a.get("start_date", ""), reverse=True)
    return [a for a in all_acts if a.get("start_date", "") >= "2025-01-01T00:00:00Z"]

def get_activity_zones(token, activity_id):
    r = requests.get(f"https://www.strava.com/api/v3/activities/{activity_id}/zones",
                     headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 200:
        for z in r.json():
            if z.get("type") == "heartrate":
                return z.get("distribution_buckets", [])
    return []

def get_best_efforts(token):
    """Get best efforts from last 10 runs only to avoid rate limit."""
    all_efforts = {}
    # Get recent activities first (newest first, no after filter)
    r = requests.get("https://www.strava.com/api/v3/athlete/activities",
                     headers={"Authorization": f"Bearer {token}"},
                     params={"per_page": 30, "page": 1})
    if r.status_code != 200:
        return []
    activities = r.json()
    runs = [a for a in activities if a.get("sport_type") == "Run"][:10]  # max 10 runs

    for run in runs:
        detail = requests.get(f"https://www.strava.com/api/v3/activities/{run['id']}",
                               headers={"Authorization": f"Bearer {token}"})
        if detail.status_code != 200:
            continue
        for effort in detail.json().get("best_efforts", []):
            name    = effort.get("name", "")
            elapsed = effort.get("elapsed_time", 0)
            date    = effort.get("start_date_local", "")[:10]
            dist    = effort.get("distance", 0)
            if name not in all_efforts or elapsed < all_efforts[name]["elapsed_time"]:
                all_efforts[name] = {
                    "name": name, "elapsed_time": elapsed,
                    "distance": dist, "date": date,
                    "activity": run.get("name", "")
                }
    return list(all_efforts.values())

def classify_zone(avg_hr):
    if not avg_hr: return None
    for i, (lo, hi) in enumerate([(0,124),(124,154),(154,169),(169,184),(184,999)]):
        if lo <= avg_hr < hi: return i + 1
    return 5

def ask_gemini(recent10, goals=None):
    """AI analysis via Gemini 2.0 Flash — free, 1500 req/day, no token cap."""
    lines = []
    for i, a in enumerate(recent10):
        sport = a.get("sport_type", "Unknown")
        dist  = round(a.get("distance", 0) / 1000, 2)
        dur   = round(a.get("moving_time", 0) / 60, 1)
        hr    = a.get("average_heartrate", "N/A")
        pace  = round(dur / dist, 2) if dist > 0 else "N/A"
        lines.append(f'{i}. [{a.get("start_date_local","")[:10]}] {sport} "{a.get("name","")}" | {dist}km {dur}min pace:{pace} HR:{hr}')

    goals_txt = ""
    if goals:
        gp = []
        if goals.get("race_date") and goals.get("race_dist"):
            gp.append(f"Next race: {goals['race_dist']} on {goals['race_date']}{(' goal: ' + goals['race_time']) if goals.get('race_time') else ''}")
        if goals.get("weekly_km"):   gp.append(f"Weekly km goal: {goals['weekly_km']}km")
        if goals.get("weekly_runs"): gp.append(f"Weekly runs goal: {goals['weekly_runs']}")
        if goals.get("pr_5k_goal"):  gp.append(f"5K goal: {goals['pr_5k_goal']}")
        if goals.get("pr_10k_goal"): gp.append(f"10K goal: {goals['pr_10k_goal']}")
        if goals.get("pr_21k_goal"): gp.append(f"Half goal: {goals['pr_21k_goal']}")
        if goals.get("pr_42k_goal"): gp.append(f"Marathon goal: {goals['pr_42k_goal']}")
        if gp: goals_txt = "\n\nGoals:\n" + "\n".join(gp)

    prompt = f"""Analyze these 10 most recent Strava workouts:{goals_txt}

{chr(10).join(lines)}

HR Zones: Z1<124(Recovery), Z2 124-154(Endurance), Z3 154-169(Tempo), Z4 169-184(Threshold), Z5 185+(Max)
Focus on VO2 max improvement and pace improvement in your analysis and next workout recommendation.

Return ONLY valid JSON (no markdown, no code fences):
{{
  "workouts": [{{"index":0,"highlight":"one sentence what was done well (use you)","description":"2 sentences on this workout value (use you)","comparison":"compare to others (use you)"}}],
  "summary": "3-4 sentences on these 10 workouts — volume, consistency, HR zones (use you)",
  "analysis": "2-3 paragraphs coaching analysis focused on VO2 max and pace improvement{' referencing goals' if goals_txt else ''} (use you)",
  "next_workout": {{"type":"Run","title":"workout name","description":"3-4 sentences recommending next workout targeting VO2 max or pace improvement (use you)"}}
}}"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.7, "maxOutputTokens": 3500}}

    # Retry up to 4 times with backoff for rate limit errors
    import time
    for attempt in range(4):
        resp = requests.post(url, json=payload, timeout=60)
        if resp.status_code == 429:
            wait = 2 ** attempt * 5  # 5s, 10s, 20s, 40s
            print(f"Gemini 429 rate limit — waiting {wait}s (attempt {attempt+1}/4)")
            time.sleep(wait)
            continue
        if resp.status_code == 404:
            # Fallback to gemini-2.0-flash if 2.5 not available
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
            resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        break
    else:
        raise Exception("Gemini rate limit exceeded after 4 retries. Please wait a minute and try again.")
    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    raw = raw.replace("```json","").replace("```","").strip()
    s, e = raw.find('{'), raw.rfind('}') + 1
    if s >= 0 and e > s: raw = raw[s:e]
    try:
        return json.loads(raw)
    except Exception as ex:
        print(f"JSON parse error: {ex}\nRaw[:400]: {raw[:400]}")
        m = re.search(r'"summary"\s*:\s*"([^"]{10,})"', raw)
        return {
            "workouts": [],
            "summary": m.group(1) if m else "Tap Analyze again for full analysis.",
            "analysis": "Analysis unavailable — tap Analyze again.",
            "next_workout": {"type":"Run","title":"Tempo Intervals","description":"Run 4x1km at tempo pace (Z3) with 90s rest between. This targets VO2 max and pace improvement directly."}
        }

def read_file(path):
    with open(path) as f: return f.read()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]} {args[1]}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(read_file("app_v13.html").encode())
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/analyze":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length) if length else b'{}'
                try:    goals = json.loads(body).get("goals", {})
                except: goals = {}

                token    = get_access_token()
                all_acts = get_all_activities(token)  # newest first
                recent10 = all_acts[:10]

                ai = ask_gemini(recent10, goals)

                activity_list = []
                for i, a in enumerate(all_acts):
                    avg_hr  = a.get("average_heartrate")
                    # Skip zone API call — zones estimated in frontend
                    wi = next((w for w in ai.get("workouts", []) if w.get("index") == i), {})
                    activity_list.append({
                        "id":          a["id"],
                        "date":        a.get("start_date_local", "")[:10],
                        "name":        a.get("name", "Unknown"),
                        "sport":       a.get("sport_type", "Unknown"),
                        "distance":    round(a.get("distance", 0) / 1000, 2),
                        "duration":    round(a.get("moving_time", 0) / 60, 1),
                        "avg_hr":      avg_hr,
                        "max_hr":      a.get("max_heartrate"),
                        "zone":        classify_zone(avg_hr),
                        "highlight":   wi.get("highlight", ""),
                        "description": wi.get("description", ""),
                        "comparison":  wi.get("comparison", ""),
                        "calories":    a.get("calories", 0),
                    })

                try:    best_efforts = get_best_efforts(token)
                except: best_efforts = []

                runs = [a for a in all_acts if a.get("sport_type") == "Run"]
                wts  = [a for a in all_acts if a.get("sport_type") == "WeightTraining"]

                result = {
                    "success":      True,
                    "activities":   activity_list,   # newest first
                    "summary":      ai.get("summary", ""),
                    "analysis":     ai.get("analysis", ""),
                    "next_workout": ai.get("next_workout", {}),
                    "best_efforts": best_efforts,
                    "stats": {
                        "total_runs":       len(runs),
                        "total_km":         round(sum(a.get("distance",0) for a in runs)/1000, 1),
                        "total_activities": len(all_acts),
                        "weight_sessions":  len(wts),
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
    try:    local_ip = socket.gethostbyname(socket.gethostname())
    except: local_ip = "localhost"
    port = int(os.environ.get("PORT", 8080))
    print(f"\n🚴 TrainAI Server v13 — Gemini Flash")
    print(f"─" * 40)
    print(f"✅ Running on port {port}")
    print(f"📱 http://{local_ip}:{port}")
    print(f"─" * 40)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
