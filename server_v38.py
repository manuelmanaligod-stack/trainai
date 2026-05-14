"""
Gear 2 Server v38 — SQLite DB + Smart Sync
- Activities stored in SQLite (survives restarts)
- Groq only called when new workouts detected
- Strava delta sync (only fetches new activities)
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, requests, os, re, time, sqlite3
from datetime import datetime
from groq import Groq

STRAVA_CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID",     "234502")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "ce8eedc1d05808af8cfb2829c833abb5d69dc9f4")
STRAVA_REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN", "2ac675776dea85002172508cdeb748ec0fe1637c")
GROQ_API_KEY         = os.environ.get("GROQ_API_KEY",         "")
GROQ_API_KEY_2       = os.environ.get("GROQ_API_KEY_2",       "")
DB_FILE              = os.environ.get("DB_FILE",               "gear2.db")
AI_TTL               = 6 * 3600  # Re-run Groq if AI is older than 6hrs AND new workouts exist

# ── DATABASE SETUP ────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS activities (
            id          INTEGER PRIMARY KEY,
            strava_id   INTEGER UNIQUE,
            date        TEXT,
            name        TEXT,
            sport       TEXT,
            distance    REAL,
            duration    REAL,
            avg_hr      REAL,
            max_hr      REAL,
            zone        INTEGER,
            calories    REAL,
            elev        REAL,
            highlight   TEXT DEFAULT '',
            description TEXT DEFAULT '',
            comparison  TEXT DEFAULT '',
            hr_zones    TEXT DEFAULT NULL,
            start_time  TEXT DEFAULT NULL,
            synced_at   TEXT
        );
        CREATE TABLE IF NOT EXISTS ai_cache (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            summary     TEXT,
            analysis    TEXT,
            next_workout TEXT,
            created_at  REAL
        );
        CREATE TABLE IF NOT EXISTS best_efforts (
            id          INTEGER PRIMARY KEY,
            name        TEXT UNIQUE,
            elapsed_time REAL,
            distance    REAL,
            date        TEXT,
            activity    TEXT
        );
        CREATE TABLE IF NOT EXISTS goals (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            last_sync   REAL,
            newest_date TEXT
        );
        """)
        # Migration: add hr_zones column to existing DB (no-op if already present)
        try:
            conn.execute("ALTER TABLE activities ADD COLUMN hr_zones TEXT DEFAULT NULL")
            print("[DB] Added hr_zones column.")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE activities ADD COLUMN start_time TEXT DEFAULT NULL")
            print("[DB] Added start_time column.")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE activities ADD COLUMN splits TEXT DEFAULT NULL")
            print("[DB] Added splits column.")
        except sqlite3.OperationalError:
            pass  # column already exists
    print("[DB] Initialized.")

# ── STRAVA ────────────────────────────────────────────────────────────
def get_access_token():
    r = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": STRAVA_CLIENT_ID, "client_secret": STRAVA_CLIENT_SECRET,
        "refresh_token": STRAVA_REFRESH_TOKEN, "grant_type": "refresh_token"
    })
    r.raise_for_status()
    return r.json()["access_token"]

def classify_zone(avg_hr):
    if not avg_hr: return None
    for i, (lo, hi) in enumerate([(0,124),(124,154),(154,169),(169,184),(184,999)]):
        if lo <= avg_hr < hi: return i + 1
    return 5

ZONE_THRESHOLDS = [(0,124),(124,154),(154,169),(169,184),(184,999)]

def fetch_activity_splits(token, strava_id):
    """Fetch per-km splits from Strava's /activities/{id} detail endpoint.
    Returns (json_str, status). Status: ok | no_splits | limited | error."""
    try:
        r = requests.get(
            f"https://www.strava.com/api/v3/activities/{strava_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        )
        if r.status_code == 429:
            return None, 'limited'
        if r.status_code != 200:
            return None, 'error'
        splits = r.json().get("splits_metric", [])
        if not splits:
            return None, 'no_splits'
        compact = [{
            "n": s.get("split"),
            "distance": round(s.get("distance", 0), 2),
            "time": s.get("moving_time", 0),
            "elev": round(s.get("elevation_difference", 0), 1),
            "hr": round(s.get("average_heartrate"), 0) if s.get("average_heartrate") else None,
            "speed": s.get("average_speed"),
            "pace_zone": s.get("pace_zone"),
        } for s in splits]
        return json.dumps(compact), 'ok'
    except Exception as e:
        print(f"[Splits] Failed for {strava_id}: {e}")
        return None, 'error'

def fetch_activity_zones(token, strava_id):
    """Compute exact HR time-in-zone for one activity from Strava's free
    /streams endpoint (per-second HR samples).
    Returns (json_str, status) where status is one of:
      'ok'      — zones computed, json_str is the [s_z1..s_z5] data
      'no_hr'   — request OK but no heartrate stream present
      'limited' — Strava rate-limited (429); caller should stop
      'missing' — 404 / activity not found
      'paid'    — 402 (premium required — shouldn't happen with streams)
      'error'   — other failure"""
    try:
        r = requests.get(
            f"https://www.strava.com/api/v3/activities/{strava_id}/streams",
            headers={"Authorization": f"Bearer {token}"},
            params={"keys": "heartrate,time", "key_by_type": "true"},
            timeout=12
        )
        if r.status_code == 429:
            return None, 'limited'
        if r.status_code == 404:
            return None, 'missing'
        if r.status_code == 402:
            return None, 'paid'
        if r.status_code != 200:
            return None, 'error'

        data = r.json()
        hr_stream = data.get("heartrate", {}).get("data")
        time_stream = data.get("time", {}).get("data")
        if not hr_stream:
            return None, 'no_hr'

        # Count seconds in each zone.
        # If we have a time stream, use deltas between samples; otherwise
        # assume samples are ~1 second apart (Strava's default resolution).
        times = [0, 0, 0, 0, 0]
        if time_stream and len(time_stream) == len(hr_stream):
            for i, hr in enumerate(hr_stream):
                if hr is None or hr <= 0:
                    continue
                if i + 1 < len(time_stream):
                    dt = max(0, time_stream[i+1] - time_stream[i])
                else:
                    dt = 1
                # cap weird gaps (paused recordings) at 5s
                if dt > 5:
                    dt = 1
                for zi, (lo, hi) in enumerate(ZONE_THRESHOLDS):
                    if lo <= hr < hi:
                        times[zi] += dt
                        break
        else:
            for hr in hr_stream:
                if hr is None or hr <= 0:
                    continue
                for zi, (lo, hi) in enumerate(ZONE_THRESHOLDS):
                    if lo <= hr < hi:
                        times[zi] += 1
                        break

        if sum(times) > 0:
            return json.dumps([int(t) for t in times]), 'ok'
        return None, 'no_hr'
    except Exception as e:
        print(f"[Zones] Failed for {strava_id}: {e}")
        return None, 'error'

def fetch_new_activities(token, after_date=None):
    """Fetch only activities newer than after_date. If None, fetch all 2025+."""
    all_acts = []
    after_ts = None
    if after_date:
        from datetime import datetime as dt
        after_ts = int(dt.strptime(after_date, "%Y-%m-%d").timestamp())

    for page in range(1, 11):
        params = {"per_page": 30, "page": page}
        if after_ts: params["after"] = after_ts
        r = requests.get("https://www.strava.com/api/v3/athlete/activities",
                         headers={"Authorization": f"Bearer {token}"},
                         params=params)
        if r.status_code != 200: break
        batch = r.json()
        if not batch: break
        all_acts.extend(batch)
        oldest = batch[-1].get("start_date", "9999")
        if not after_ts and oldest < "2025-01-01T00:00:00Z": break

    all_acts.sort(key=lambda a: a.get("start_date", ""), reverse=True)
    if not after_ts:
        all_acts = [a for a in all_acts if a.get("start_date","") >= "2025-01-01T00:00:00Z"]
    return all_acts

def sync_activities_to_db(token, force=False):
    """Smart sync: only fetch new activities since last sync."""
    with get_db() as conn:
        log = conn.execute("SELECT * FROM sync_log WHERE id=1").fetchone()
        newest_in_db = log["newest_date"] if log else None

    if force or not newest_in_db:
        print("[Sync] Full sync from Strava...")
        acts = fetch_new_activities(token, after_date=None)
    else:
        print(f"[Sync] Delta sync since {newest_in_db}...")
        acts = fetch_new_activities(token, after_date=newest_in_db)

    if not acts:
        print("[Sync] No new activities.")
        return 0

    saved = 0
    with get_db() as conn:
        for a in acts:
            avg_hr = a.get("average_heartrate")
            sdl = a.get("start_date_local","")
            sport = a.get("sport_type","Unknown")
            # Fetch exact HR zones from Strava (only if activity has HR data)
            hr_zones_json = None
            if avg_hr:
                hr_zones_json, _status = fetch_activity_zones(token, a["id"])
            # Fetch splits for May 2026 runs
            splits_json = None
            if sdl.startswith("2026-05") and sport in ("Run", "VirtualRun"):
                splits_json, _s = fetch_activity_splits(token, a["id"])
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO activities
                    (strava_id,date,name,sport,distance,duration,avg_hr,max_hr,zone,calories,elev,hr_zones,start_time,synced_at,splits)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    a["id"],
                    sdl[:10],
                    a.get("name","Unknown"),
                    sport,
                    round(a.get("distance",0)/1000, 2),
                    round(a.get("moving_time",0)/60, 1),
                    avg_hr,
                    a.get("max_heartrate"),
                    classify_zone(avg_hr),
                    a.get("calories",0),
                    a.get("total_elevation_gain",0),
                    hr_zones_json,
                    sdl[11:16] if len(sdl) >= 16 else None,  # "HH:MM"
                    datetime.now().isoformat(),
                    splits_json,
                ))
                saved += 1
            except: pass

        # Update sync log
        newest = acts[0].get("start_date_local","")[:10] if acts else newest_in_db
        conn.execute("""
            INSERT OR REPLACE INTO sync_log (id, last_sync, newest_date)
            VALUES (1, ?, ?)
        """, (time.time(), newest))

    print(f"[Sync] Saved {saved} activities. Newest: {newest}")
    return saved

def get_activities_from_db():
    """Return all activities sorted newest first."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM activities
            WHERE date >= '2025-01-01'
            ORDER BY date DESC, strava_id DESC
        """).fetchall()
    return [dict(r) for r in rows]

def get_best_efforts_from_db(token):
    """Fetch best efforts from last 10 runs, store in DB."""
    with get_db() as conn:
        existing = conn.execute("SELECT COUNT(*) as n FROM best_efforts").fetchone()["n"]
        if existing > 0:
            rows = conn.execute("SELECT * FROM best_efforts").fetchall()
            return [dict(r) for r in rows]

    # First time — fetch from Strava (skip if no token in fast-path mode)
    if not token:
        return []
    try:
        all_efforts = {}
        r = requests.get("https://www.strava.com/api/v3/athlete/activities",
                         headers={"Authorization": f"Bearer {token}"},
                         params={"per_page": 30, "page": 1})
        if r.status_code != 200: return []
        runs = [a for a in r.json() if a.get("sport_type") == "Run"][:10]

        for run in runs:
            detail = requests.get(f"https://www.strava.com/api/v3/activities/{run['id']}",
                                   headers={"Authorization": f"Bearer {token}"})
            if detail.status_code != 200: continue
            for effort in detail.json().get("best_efforts", []):
                name = effort.get("name","")
                elapsed = effort.get("elapsed_time",0)
                if name not in all_efforts or elapsed < all_efforts[name]["elapsed_time"]:
                    all_efforts[name] = {
                        "name": name, "elapsed_time": elapsed,
                        "distance": effort.get("distance",0),
                        "date": effort.get("start_date_local","")[:10],
                        "activity": run.get("name","")
                    }

        with get_db() as conn:
            for e in all_efforts.values():
                conn.execute("""
                    INSERT OR REPLACE INTO best_efforts (name,elapsed_time,distance,date,activity)
                    VALUES (?,?,?,?,?)
                """, (e["name"],e["elapsed_time"],e["distance"],e["date"],e["activity"]))

        return list(all_efforts.values())
    except Exception as ex:
        print(f"[BestEfforts] Error: {ex}")
        return []

# ── AI CACHE ──────────────────────────────────────────────────────────
def get_ai_cache():
    with get_db() as conn:
        row = conn.execute("SELECT * FROM ai_cache WHERE id=1").fetchone()
        return dict(row) if row else None

def save_ai_cache(summary, analysis, next_workout):
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO ai_cache (id, summary, analysis, next_workout, created_at)
            VALUES (1, ?, ?, ?, ?)
        """, (summary, analysis, json.dumps(next_workout), time.time()))

def ai_cache_age():
    cache = get_ai_cache()
    if not cache: return 99 * 24 * 3600  # treat as "very old" (99 days) — finite so int() works
    return time.time() - cache["created_at"]

# ── GROQ AI ───────────────────────────────────────────────────────────
def ask_groq(recent10, goals=None):
    lines = []
    for i, a in enumerate(recent10):
        dist = a.get("distance",0)
        dur  = a.get("duration",0)
        pace = round(dur/dist, 2) if dist > 0 else "N/A"
        lines.append(f'{i}. [{a.get("date","")}] {a.get("sport","")} "{a.get("name","")} | {dist}km {dur}min pace:{pace} HR:{a.get("avg_hr","N/A")}')

    goals_txt = ""
    if goals:
        gp = []
        if goals.get("race_date") and goals.get("race_dist"):
            gp.append(f"Next race: {goals['race_dist']} on {goals['race_date']}{(' goal: ' + goals['race_time']) if goals.get('race_time') else ''}")
        if goals.get("weekly_km"):   gp.append(f"Weekly km goal: {goals['weekly_km']}km")
        if goals.get("weekly_runs"): gp.append(f"Weekly runs goal: {goals['weekly_runs']}")
        if gp: goals_txt = "\n\nGoals:\n" + "\n".join(gp)

    prompt = f"""Analyze these 10 most recent workouts:{goals_txt}

{chr(10).join(lines)}

HR Zones: Z1<124(Recovery), Z2 124-154(Endurance), Z3 154-169(Tempo), Z4 169-184(Threshold), Z5 185+(Max)
Focus on VO2 max improvement and pace improvement.

Return ONLY valid JSON (no markdown):
{{
  "workouts": [{{"index":0,"highlight":"one sentence (use you)","description":"2 sentences (use you)","comparison":"compare to others (use you)"}}],
  "summary": "3-4 sentences on volume, consistency, HR zones (use you)",
  "analysis": "2-3 paragraphs on VO2 max and pace improvement{" referencing goals" if goals_txt else ""} (use you)",
  "next_workout": {{"type":"Run","title":"workout name","description":"3-4 sentences (use you)"}}
}}"""

    fallback = {
        "workouts": [], "summary": "Tap Analyze again.",
        "analysis": "Analysis unavailable.", 
        "next_workout": {"type":"Run","title":"Tempo Run","description":"4x1km at threshold pace with 90s rest."}
    }

    for key_name, api_key in [("primary", GROQ_API_KEY), ("backup", GROQ_API_KEY_2)]:
        try:
            print(f"[Groq] Trying {key_name} key...")
            client = Groq(api_key=api_key)
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role":"user","content":prompt}],
                max_tokens=3500
            )
            raw = resp.choices[0].message.content.replace("```json","").replace("```","").strip()
            raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', raw)
            raw = raw.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
            s, e = raw.find('{'), raw.rfind('}') + 1
            if s >= 0 and e > s: raw = raw[s:e]
            result = json.loads(raw)
            if isinstance(result, dict):
                print(f"[Groq] {key_name} succeeded.")
                return result
            return fallback
        except Exception as ex:
            err = str(ex)
            if "429" in err or "rate_limit" in err.lower():
                print(f"[Groq] {key_name} rate limited. {'Trying backup...' if key_name == 'primary' else 'Both exhausted.'}")
                if key_name == "backup": raise Exception(f"Error code: {err}")
            else:
                print(f"[Groq] {key_name} error: {err}")
                return fallback
    return fallback

def update_activity_ai(workouts_ai):
    """Store AI highlights back into the activities table."""
    with get_db() as conn:
        for w in workouts_ai:
            idx = w.get("index")
            if idx is None: continue
            # Get the idx-th activity
            row = conn.execute(
                "SELECT strava_id FROM activities WHERE date >= '2025-01-01' ORDER BY date DESC, strava_id DESC LIMIT 1 OFFSET ?",
                (idx,)
            ).fetchone()
            if row:
                conn.execute("""
                    UPDATE activities SET highlight=?, description=?, comparison=?
                    WHERE strava_id=?
                """, (w.get("highlight",""), w.get("description",""), w.get("comparison",""), row["strava_id"]))

GOALS_FILE = "goals_backup.json"

def load_goals_from_file():
    try:
        if os.path.exists(GOALS_FILE):
            with open(GOALS_FILE) as f: return json.load(f)
    except: pass
    return {}

def save_goals_to_file(goals):
    try:
        with open(GOALS_FILE, "w") as f: json.dump(goals, f)
    except: pass

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
            self.wfile.write(read_file("app_v36.html").encode())
        elif self.path.startswith("/icons/"):
            file_path = self.path.lstrip("/")
            if os.path.exists(file_path):
                self.send_response(200)
                self.send_header("Content-type", "image/svg+xml")
                self.end_headers()
                with open(file_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path == "/export":
            # Build a self-contained offline HTML with all data baked in.
            try:
                # Pull everything from the DB without touching Strava
                with get_db() as conn:
                    rows = conn.execute(
                        "SELECT * FROM activities WHERE date >= '2025-01-01' ORDER BY date DESC, strava_id DESC"
                    ).fetchall()
                    be_rows = conn.execute("SELECT * FROM best_efforts").fetchall()
                    g_rows = conn.execute("SELECT key, value FROM goals").fetchall()
                    ai_row = conn.execute("SELECT * FROM ai_cache WHERE id=1").fetchone()

                activities = []
                for a in rows:
                    activities.append({
                        "id":          a["strava_id"],
                        "date":        a["date"],
                        "name":        a["name"],
                        "sport":       a["sport"],
                        "distance":    a["distance"],
                        "duration":    a["duration"],
                        "avg_hr":      a["avg_hr"],
                        "max_hr":      a["max_hr"],
                        "zone":        a["zone"],
                        "highlight":   a["highlight"] or "",
                        "description": a["description"] or "",
                        "comparison":  a["comparison"] or "",
                        "calories":    a["calories"],
                        "elev":        a["elev"],
                        "hr_zones":    json.loads(a["hr_zones"]) if a["hr_zones"] else None,
                        "start_time":  a["start_time"],
                        "splits":      json.loads(a["splits"]) if a["splits"] else None,
                    })

                best_efforts = [dict(r) for r in be_rows]
                goals = {r["key"]: r["value"] for r in g_rows}
                ai = {
                    "summary":      ai_row["summary"] if ai_row else "",
                    "analysis":     ai_row["analysis"] if ai_row else "",
                    "next_workout": json.loads(ai_row["next_workout"]) if ai_row and ai_row["next_workout"] else {},
                }

                runs = [a for a in activities if a["sport"] == "Run"]
                wts  = [a for a in activities if a["sport"] == "WeightTraining"]
                payload = {
                    "success":      True,
                    "activities":   activities,
                    "summary":      ai["summary"],
                    "analysis":     ai["analysis"],
                    "next_workout": ai["next_workout"],
                    "best_efforts": best_efforts,
                    "stats": {
                        "total_runs":       len(runs),
                        "total_km":         round(sum(a["distance"] for a in runs), 1),
                        "total_activities": len(activities),
                        "weight_sessions":  len(wts),
                    },
                    "exported_at": datetime.now().isoformat(),
                }

                html = read_file("app_v36.html")
                inject = (
                    f"<script>"
                    f"window.__OFFLINE_DATA__={json.dumps(payload)};"
                    f"window.__OFFLINE_GOALS__={json.dumps(goals)};"
                    f"</script>"
                )
                html = html.replace("</head>", inject + "</head>", 1)

                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="gear2_offline.html"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                print(f"[Export] Served offline bundle: {len(activities)} activities")
            except Exception as e:
                import traceback; traceback.print_exc()
                self.send_response(500); self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        elif self.path == "/zones_status":
            try:
                with get_db() as conn:
                    total = conn.execute("SELECT COUNT(*) AS n FROM activities").fetchone()["n"]
                    with_hr = conn.execute("SELECT COUNT(*) AS n FROM activities WHERE avg_hr IS NOT NULL").fetchone()["n"]
                    exact = conn.execute("SELECT COUNT(*) AS n FROM activities WHERE hr_zones IS NOT NULL").fetchone()["n"]
                    sample = conn.execute(
                        "SELECT date, sport, name FROM activities WHERE hr_zones IS NOT NULL ORDER BY date DESC LIMIT 10"
                    ).fetchall()
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "total": total,
                    "with_hr": with_hr,
                    "exact_strava_zones": exact,
                    "still_estimated": with_hr - exact,
                    "pct_exact": round(exact / with_hr * 100, 1) if with_hr else 0,
                    "newest_with_exact": [{"date": r["date"], "sport": r["sport"], "name": r["name"]} for r in sample]
                }, indent=2).encode())
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        elif self.path == "/goals":
            # Load from DB, fall back to file if DB is empty (Render spin-down)
            with get_db() as conn:
                rows = conn.execute("SELECT key, value FROM goals").fetchall()
            goals = {r["key"]: r["value"] for r in rows}
            if not goals:
                goals = load_goals_from_file()
                if goals:
                    print("[Goals] DB empty, restored from file backup")
                    with get_db() as conn:
                        for k,v in goals.items():
                            conn.execute("INSERT OR REPLACE INTO goals (key,value) VALUES (?,?)",(k,v))
            body = json.dumps(goals).encode()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Content-Length", len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/backfill_splits":
            # Backfill per-km splits for Run activities. Defaults to May 2026 only.
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b'{}'
                try: payload = json.loads(body)
                except: payload = {}
                month_prefix = payload.get("month", "2026-05")
                limit = int(payload.get("limit", 30))
                token = get_access_token()
                with get_db() as conn:
                    rows = conn.execute(
                        "SELECT strava_id, date, name FROM activities WHERE splits IS NULL AND sport IN ('Run','VirtualRun') AND date LIKE ? ORDER BY date DESC LIMIT ?",
                        (month_prefix + "%", limit)
                    ).fetchall()
                counts = {"ok": 0, "no_splits": 0, "limited": 0, "error": 0}
                hit_limit = False
                for r in rows:
                    sid = r["strava_id"]
                    s, status = fetch_activity_splits(token, sid)
                    counts[status] = counts.get(status, 0) + 1
                    if status == 'limited':
                        hit_limit = True
                        break
                    if s:
                        with get_db() as conn:
                            conn.execute("UPDATE activities SET splits=? WHERE strava_id=?", (s, sid))
                    time.sleep(0.1)
                print(f"[BackfillSplits] {month_prefix}: ok={counts['ok']} no_splits={counts['no_splits']} limited={counts['limited']} error={counts['error']}")
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "month": month_prefix, "counts": counts, "hit_rate_limit": hit_limit}).encode())
            except Exception as e:
                import traceback; traceback.print_exc()
                self.send_response(500); self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        if self.path == "/backfill_times":
            # Re-fetch /athlete/activities and update start_time for existing rows.
            # Strava's list endpoint is cheap (one page covers ~30 activities).
            try:
                token = get_access_token()
                updated = 0
                pages_done = 0
                for page in range(1, 30):
                    r = requests.get(
                        "https://www.strava.com/api/v3/athlete/activities",
                        headers={"Authorization": f"Bearer {token}"},
                        params={"per_page": 30, "page": page},
                        timeout=10
                    )
                    if r.status_code != 200:
                        print(f"[BackfillTimes] page {page} status {r.status_code}, stopping")
                        break
                    batch = r.json()
                    if not batch:
                        break
                    pages_done += 1
                    with get_db() as conn:
                        for a in batch:
                            sdl = a.get("start_date_local", "")
                            t = sdl[11:16] if len(sdl) >= 16 else None
                            if t:
                                cur = conn.execute(
                                    "UPDATE activities SET start_time=? WHERE strava_id=? AND (start_time IS NULL OR start_time='')",
                                    (t, a["id"])
                                )
                                if cur.rowcount > 0:
                                    updated += 1
                    oldest = batch[-1].get("start_date","9999")
                    if oldest < "2025-01-01T00:00:00Z":
                        break
                    time.sleep(0.1)
                with get_db() as conn:
                    missing = conn.execute(
                        "SELECT COUNT(*) AS n FROM activities WHERE start_time IS NULL"
                    ).fetchone()["n"]
                print(f"[BackfillTimes] Updated {updated} rows across {pages_done} pages. {missing} still missing.")
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "updated": updated, "pages_fetched": pages_done, "still_missing": missing}).encode())
            except Exception as e:
                import traceback; traceback.print_exc()
                self.send_response(500); self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        if self.path == "/backfill_zones":
            # Backfill exact HR zones for existing activities that don't have them.
            # Stops early on Strava rate limit (429). Reports per-status counts.
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b'{}'
                try: payload = json.loads(body)
                except: payload = {}
                limit = int(payload.get("limit", 30))

                token = get_access_token()
                with get_db() as conn:
                    rows = conn.execute(
                        "SELECT strava_id, date, name FROM activities WHERE hr_zones IS NULL AND avg_hr IS NOT NULL ORDER BY date DESC LIMIT ?",
                        (limit,)
                    ).fetchall()

                counts = {"ok": 0, "no_hr": 0, "missing": 0, "error": 0, "limited": 0, "paid": 0}
                hit_limit = False
                processed = 0
                for r in rows:
                    sid = r["strava_id"]
                    z, status = fetch_activity_zones(token, sid)
                    counts[status] = counts.get(status, 0) + 1
                    processed += 1
                    if status == 'limited':
                        print(f"[Backfill] Rate limited at activity {sid} ({r['date']} {r['name']}). Stopping early.")
                        hit_limit = True
                        break
                    if z:
                        with get_db() as conn:
                            conn.execute("UPDATE activities SET hr_zones=? WHERE strava_id=?", (z, sid))
                    time.sleep(0.05)  # gentle pacing

                # Remaining count after this run
                with get_db() as conn:
                    remaining = conn.execute(
                        "SELECT COUNT(*) AS n FROM activities WHERE hr_zones IS NULL AND avg_hr IS NOT NULL"
                    ).fetchone()["n"]
                    total_exact = conn.execute(
                        "SELECT COUNT(*) AS n FROM activities WHERE hr_zones IS NOT NULL"
                    ).fetchone()["n"]

                print(f"[Backfill] Processed {processed}/{len(rows)}. "
                      f"ok={counts['ok']} no_hr={counts['no_hr']} missing={counts['missing']} "
                      f"error={counts['error']} limited={counts['limited']}. "
                      f"Remaining to backfill: {remaining}. Total exact in DB: {total_exact}")

                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "ok": True,
                    "processed": processed,
                    "requested": len(rows),
                    "counts": counts,
                    "hit_rate_limit": hit_limit,
                    "remaining": remaining,
                    "total_exact_in_db": total_exact,
                    "hint": "Wait 15 minutes if hit_rate_limit is true, then call again." if hit_limit else ("All done! No more activities need backfilling." if remaining == 0 else f"{remaining} activities still need backfilling — call again to continue.")
                }).encode())
            except Exception as e:
                import traceback; traceback.print_exc()
                self.send_response(500); self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        if self.path == "/zones_status":
            # Quick read-only status of zone backfill progress
            try:
                with get_db() as conn:
                    total = conn.execute("SELECT COUNT(*) AS n FROM activities").fetchone()["n"]
                    with_hr = conn.execute("SELECT COUNT(*) AS n FROM activities WHERE avg_hr IS NOT NULL").fetchone()["n"]
                    exact = conn.execute("SELECT COUNT(*) AS n FROM activities WHERE hr_zones IS NOT NULL").fetchone()["n"]
                    missing = with_hr - exact
                    sample = conn.execute(
                        "SELECT date, sport, name FROM activities WHERE hr_zones IS NOT NULL ORDER BY date DESC LIMIT 5"
                    ).fetchall()
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "total": total,
                    "with_hr": with_hr,
                    "exact_strava_zones": exact,
                    "still_estimated": missing,
                    "pct_exact": round(exact / with_hr * 100, 1) if with_hr else 0,
                    "newest_with_exact": [{"date": r["date"], "sport": r["sport"], "name": r["name"]} for r in sample]
                }).encode())
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        if self.path == "/goals":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length) if length else b'{}'
                goals  = json.loads(body)
                with get_db() as conn:
                    for k, v in goals.items():
                        conn.execute("INSERT OR REPLACE INTO goals (key,value) VALUES (?,?)", (k, v))
                save_goals_to_file(goals)  # also save to file for spin-down resilience
                print(f"[Goals] Saved {len(goals)} keys")
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self.send_response(500); self.end_headers()
            return

        if self.path == "/analyze":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length) if length else b'{}'
                try:    payload = json.loads(body)
                except: payload = {}
                goals         = payload.get("goals", {})
                force_refresh = payload.get("force_refresh", False)

                # Fast path: if DB was synced within the last 5 minutes and the
                # caller isn't forcing a refresh, skip Strava OAuth + delta probe
                # entirely. Saves ~1-2s on warm requests.
                SKIP_SYNC_WINDOW = 5 * 60  # seconds
                with get_db() as conn:
                    log = conn.execute("SELECT last_sync FROM sync_log WHERE id=1").fetchone()
                last_sync_age = (time.time() - float(log["last_sync"])) if log and log["last_sync"] else 1e9

                if not force_refresh and last_sync_age < SKIP_SYNC_WINDOW:
                    print(f"[Analyze] Fast path — DB synced {int(last_sync_age)}s ago, skipping Strava")
                    token = None
                    new_count = 0
                else:
                    token = get_access_token()
                    # Smart sync: check for new activities
                    new_count = sync_activities_to_db(token, force=force_refresh)

                all_acts  = get_activities_from_db()
                recent10  = all_acts[:10]

                # Decide whether to call Groq
                cache_age = ai_cache_age()
                cached_ai = get_ai_cache()
                ai_empty  = not cached_ai or not cached_ai.get("summary") or cached_ai.get("summary") == "Tap Analyze again."
                has_new   = new_count > 0
                need_ai   = force_refresh or has_new or cache_age > AI_TTL or ai_empty

                if need_ai:
                    reason = "force refresh" if force_refresh else f"{new_count} new workouts" if has_new else f"AI cache {int(cache_age/3600)}h old"
                    print(f"[AI] Running Groq ({reason})...")
                    ai = ask_groq(recent10, goals)
                    save_ai_cache(ai.get("summary",""), ai.get("analysis",""), ai.get("next_workout",{}))
                    update_activity_ai(ai.get("workouts",[]))
                else:
                    print(f"[AI] Skipping Groq — no new workouts, cache {int(cache_age/60)}m old.")
                    cached_ai = get_ai_cache()
                    ai = {
                        "summary":      cached_ai["summary"],
                        "analysis":     cached_ai["analysis"],
                        "next_workout": json.loads(cached_ai["next_workout"]),
                        "workouts":     []
                    }

                # Reload activities with updated AI highlights
                all_acts = get_activities_from_db()

                activity_list = []
                for a in all_acts:
                    activity_list.append({
                        "id":          a["strava_id"],
                        "date":        a["date"],
                        "name":        a["name"],
                        "sport":       a["sport"],
                        "distance":    a["distance"],
                        "duration":    a["duration"],
                        "avg_hr":      a["avg_hr"],
                        "max_hr":      a["max_hr"],
                        "zone":        a["zone"],
                        "highlight":   a.get("highlight",""),
                        "description": a.get("description",""),
                        "comparison":  a.get("comparison",""),
                        "calories":    a["calories"],
                        "elev":        a["elev"],
                        "hr_zones":    json.loads(a["hr_zones"]) if a.get("hr_zones") else None,
                        "start_time":  a.get("start_time"),
                        "splits":      json.loads(a["splits"]) if a.get("splits") else None,
                    })

                try:    best_efforts = get_best_efforts_from_db(token)
                except: best_efforts = []

                runs = [a for a in all_acts if a["sport"] == "Run"]
                wts  = [a for a in all_acts if a["sport"] == "WeightTraining"]

                result = {
                    "success":      True,
                    "activities":   activity_list,
                    "summary":      ai.get("summary",""),
                    "analysis":     ai.get("analysis",""),
                    "next_workout": ai.get("next_workout",{}),
                    "best_efforts": best_efforts,
                    "stats": {
                        "total_runs":       len(runs),
                        "total_km":         round(sum(a["distance"] for a in runs), 1),
                        "total_activities": len(all_acts),
                        "weight_sessions":  len(wts),
                    }
                }

            except Exception as e:
                import traceback
                result = {"success": False, "error": str(e), "trace": traceback.format_exc()}

            body_out = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Content-Length", len(body_out))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body_out)

def boot_sync_if_empty():
    """If the DB is brand-new/empty (e.g. after a Render deploy wiped it),
    kick off an initial sync in a background thread. Probes Strava's read
    rate-limit header first and bails out if we're already at the daily cap
    so we don't waste boot cycles hitting 429s."""
    try:
        with get_db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        if n > 0:
            return
        import threading
        def _sync():
            try:
                token = get_access_token()
                # Cheap probe: ask Strava for 1 activity. If 429, abort so we don't
                # blow through more rate-limit budget on a doomed sync attempt.
                probe = requests.get(
                    "https://www.strava.com/api/v3/athlete/activities",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"per_page": 1, "page": 1},
                    timeout=15
                )
                if probe.status_code == 429:
                    print(f"[Boot] Strava rate-limited (read usage: {probe.headers.get('x-readratelimit-usage','?')}) — deferring sync")
                    return
                added = sync_activities_to_db(token, force=True)
                print(f"[Boot] Auto-synced {added} activities to fresh DB")
            except Exception as e:
                print(f"[Boot] Auto-sync failed: {e}")
        threading.Thread(target=_sync, daemon=True).start()
        print("[Boot] DB empty — probing Strava before sync")
    except Exception as e:
        print(f"[Boot] Skipping auto-sync: {e}")

if __name__ == "__main__":
    import socket
    init_db()
    boot_sync_if_empty()
    try:    local_ip = socket.gethostbyname(socket.gethostname())
    except: local_ip = "localhost"
    port = int(os.environ.get("PORT", 8080))
    print(f"\n Gear 2 Server v38 — SQLite + Smart Sync")
    print(f"─" * 40)
    print(f"✅ Running on port {port}")
    print(f"📱 http://{local_ip}:{port}")
    print(f"─" * 40)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
