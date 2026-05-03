"""
TrainAI Server v5 - Apple Health + Groq
-----------------------------------------
Accepts workout data posted from an iOS Shortcut,
stores it, and serves it to the TrainAI web app.

Run with: python3 server_v5.py
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os
from groq import Groq
from datetime import datetime

GROQ_API_KEY  = "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"
DATA_FILE     = "health_data.json"  # stores latest data from Shortcut

# ─── Groq Analysis ───────────────────────────────────────────────────────────

def ask_groq(workouts):
    lines = []
    for w in workouts:
        name     = w.get("name", "Workout")
        date     = w.get("date", "")[:10]
        duration = round(w.get("duration", 0) / 60, 1)
        cal      = w.get("calories", "N/A")
        avg_hr   = w.get("avg_hr", "N/A")
        zones    = w.get("zones", {})
        zone_str = ""
        if zones:
            zone_str = " | Zones: " + ", ".join(
                f"Z{i+1}:{round(t/60,1)}min" 
                for i,t in enumerate([
                    zones.get("z1",0), zones.get("z2",0),
                    zones.get("z3",0), zones.get("z4",0), zones.get("z5",0)
                ]) if t > 0
            )
        lines.append(f"- [{date}] {name} | {duration}min | HR:{avg_hr} | Cal:{cal}{zone_str}")

    client = Groq(api_key=GROQ_API_KEY)
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": f"""Here are my recent Apple Watch workouts with real HR zone data:

{chr(10).join(lines)}

HR Zones: Z1=Recovery, Z2=Endurance, Z3=Tempo, Z4=Threshold, Z5=Max

1. Summarize my training volume and consistency
2. Analyze my HR zone distribution - am I training too hard or easy?
3. Give 2-3 specific actionable recommendations

Be concise and encouraging."""}],
        max_tokens=800
    )
    return response.choices[0].message.content

# ─── File helpers ─────────────────────────────────────────────────────────────

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return None

def read_file(path):
    with open(path, "r") as f:
        return f.read()

# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]} {args[1]}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(read_file("app_v5.html").encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        # iOS Shortcut posts workout data here
        if self.path == "/sync":
            try:
                data = json.loads(body)
                workouts = data.get("workouts", [])
                save_data({"workouts": workouts, "synced_at": datetime.now().isoformat()})
                print(f"✅ Received {len(workouts)} workouts from iPhone")
                result = {"success": True, "received": len(workouts)}
            except Exception as e:
                result = {"success": False, "error": str(e)}

            self._json(result)

        # App requests analysis
        elif self.path == "/analyze":
            try:
                stored = load_data()
                if not stored:
                    result = {"success": False, "error": "No data yet. Run the iOS Shortcut first!"}
                else:
                    workouts   = stored["workouts"]
                    synced_at  = stored.get("synced_at", "")[:16].replace("T", " ")
                    analysis   = ask_groq(workouts)

                    # Stats
                    cardio = [w for w in workouts if w.get("name") not in ("Strength Training","Functional Strength Training","Core Training")]
                    strength = [w for w in workouts if w.get("name") in ("Strength Training","Functional Strength Training","Core Training")]
                    total_mins = sum(w.get("duration",0) for w in workouts) / 60

                    result = {
                        "success":   True,
                        "analysis":  analysis,
                        "synced_at": synced_at,
                        "workouts":  workouts,
                        "stats": {
                            "total":    len(workouts),
                            "cardio":   len(cardio),
                            "strength": len(strength),
                            "mins":     round(total_mins),
                        }
                    }
            except Exception as e:
                import traceback
                result = {"success": False, "error": str(e), "trace": traceback.format_exc()}

            self._json(result)

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import socket
    try: local_ip = socket.gethostbyname(socket.gethostname())
    except: local_ip = "localhost"
    port = 8080

    print(f"\n🍎 TrainAI Server v5 — Apple Health Edition")
    print(f"─" * 45)
    print(f"✅ Server running!")
    print(f"📱 App URL:  http://{local_ip}:{port}")
    print(f"🔗 Sync URL: http://{local_ip}:{port}/sync  ← use this in iOS Shortcut")
    print(f"─" * 45)
    print(f"Press Ctrl+C to stop\n")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
