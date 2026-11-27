"""Gemini diagnosis, provider fallbacks, CORS, registration throttling and the new CSV parser."""
import tempfile, json, subprocess, time, urllib.request, urllib.error, os, sys, signal
SP = os.path.dirname(os.path.abspath(__file__))
MAIN_PY = os.environ.get("MAIN_PY", os.path.join(SP, "..", "app", "main.py"))
API = "http://127.0.0.1:8123/api/v1"
def sa_id(yy, mm, dd, seq):
    base = f"{yy:02d}{mm:02d}{dd:02d}{seq:04d}08"
    odd = sum(int(d) for d in base[0::2]); ev = 0
    for d in base[1::2]:
        x = int(d) * 2; ev += x if x < 10 else x - 9
    return base + str((10 - (odd + ev) % 10) % 10)
def call(method, path, token=None, body=None, raw=None, ctype="application/json", headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": ctype, **(headers or {})}
    if token: h["X-Wrangler-Token"] = token
    r = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=40) as resp: return resp.status, json.loads(resp.read() or b"{}"), resp.headers
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}"), e.headers
def fake(path): return json.loads(urllib.request.urlopen("http://127.0.0.1:9001" + path).read())
def mp(content):
    b = "XBOUND"
    return (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="x.csv"\r\nContent-Type: text/csv\r\n\r\n'.encode() + content + f"\r\n--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}")
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if not cond and extra else ""))
    if not cond: fails.append(name)

BASE_ENV = dict(os.environ, SUPABASE_URL="http://127.0.0.1:9001", SUPABASE_KEY="k", PORT="8123", LOG_LEVEL="WARNING",
                GEMINI_API_BASE="http://127.0.0.1:9002/v1beta", GEMINI_API_KEY="gem-key",
                GEMINI_PER_SCHOOL_PER_HOUR="3", REGISTER_PER_IP_PER_HOUR="50", LLM_URL="")
api = None
def start_api(**env):
    global api
    stop_api()
    api = subprocess.Popen([sys.executable, MAIN_PY], env={**BASE_ENV, **env}, stdout=open(os.path.join(tempfile.gettempdir(), "apiG.log"), "w"), stderr=subprocess.STDOUT)
    time.sleep(1.0)
def stop_api():
    global api
    if api:
        api.send_signal(signal.SIGTERM); api.wait(5); api = None
def gem_mode(m): urllib.request.urlopen("http://127.0.0.1:9002/_gemini_mode?m=" + m).read()
def gem_calls(): return json.loads(urllib.request.urlopen("http://127.0.0.1:9002/_gemini").read())

I = [sa_id(10, 3, 4, n) for n in range(1, 8)]
def csvf(good, bad=0, mark=60):
    rows = [f"{I[i]},Learner{i},Test,10,Mathematics,{mark},0.9" for i in range(good)] + ["1234567890123,Bad,Id,10,Mathematics,60,0.9"] * bad
    return "\n".join(rows).encode()
def table(t): return fake("/rest/v1/" + t)
fk = subprocess.Popen([sys.executable, os.path.join(SP, "fake_pg.py")]); time.sleep(0.8)
try:
    start_api(); gem_mode("ok")
    s, a, _ = call("POST", "/register", body={"school_name": "Report High"}); T = a["token"]
    s, b, _ = call("POST", "/register", body={"school_name": "Other School"}); TB = b["token"]

    # validation counts saved on ingest
    body, ct = mp(csvf(3, 2)); s, r, _ = call("POST", "/ingest?year=2026&term=1", T, raw=body, ctype=ct)
    tr = table("sasams_term_reports"); check("ingest saves validation counts for the term", s == 200 and len(tr) == 1 and tr[0]["validation"] == {"total_rows_processed": 5, "valid_rows": 3, "invalid_rows": 2, "error_categories": {"identity_validation": 2}}, str(tr))
    body, ct = mp(csvf(3, 0)); s, _, _ = call("POST", "/validate?year=2026&term=3", T, raw=body, ctype=ct); check("dry-run validate saves nothing", len(table("sasams_term_reports")) == 1)

    # diagnose saves only when a term is given
    body, ct = mp(csvf(3, 2))
    s, d, _ = call("POST", "/diagnose", T, raw=body, ctype=ct); check("diagnose without term: not saved", s == 200 and table("sasams_term_reports")[0].get("diagnosis") is None)
    n = len(gem_calls()); s, d, _ = call("POST", "/diagnose?year=2026&term=9", T, raw=body, ctype=ct); check("bad term rejected before any AI call", s == 400 and len(gem_calls()) == n)
    s, d, _ = call("POST", "/diagnose?year=2026&term=1", T, raw=body, ctype=ct)
    tr = table("sasams_term_reports")[0]; check("diagnose with term: diagnosis saved with source + model", d["source"] == "gemini" and tr["diagnosis"]["severity"] == "medium" and tr["diagnosis_source"] == "gemini" and tr["diagnosis_model"] == "gemini-4-hydrogen", str(tr)[:300])
    body, ct = mp(csvf(3, 2)); call("POST", "/ingest?year=2026&term=1", T, raw=body, ctype=ct)
    tr = table("sasams_term_reports"); check("re-ingest keeps the saved diagnosis (merge, no overwrite)", len(tr) == 1 and tr[0]["diagnosis"] is not None and tr[0]["validation"]["invalid_rows"] == 2)

    # term 2 + overdue intervention
    body, ct = mp(csvf(3, 0, 35)); call("POST", "/ingest?year=2026&term=2", T, raw=body, ctype=ct)
    s, iv, _ = call("POST", "/interventions", T, body={"id_number": I[0], "title": "Maths catch-up", "due_date": "2020-01-01"}); check("intervention created", s == 201)
    call("POST", "/interventions", T, body={"id_number": I[1], "title": "Future one", "due_date": "2999-01-01"})
    n = len(gem_calls()); s, rp, _ = call("GET", "/report", T)
    check("report defaults to the newest term", s == 200 and rp["summary"]["period"]["label"] == "2026 T2" and rp["school_name"] == "Report High" and rp["generated_at"], str(rp)[:200])
    check("report bundles recommendations and interventions with names", len(rp["recommendations"]) >= 1 and len(rp["interventions"]) == 2 and rp["interventions"][0]["name"].startswith("Learner"))
    check("overdue count only counts active + past due", rp["overdue_count"] == 1, str(rp["overdue_count"]))
    check("T2 has validation? (no ingest errors) but no diagnosis yet", rp["saved"] is not None and rp["saved"].get("diagnosis") is None and rp["narrative"] is None)
    check("report never spends an AI call", len(gem_calls()) == n)
    s, rp1, _ = call("GET", "/report?year=2026&term=1", T); check("term 1 report carries the saved diagnosis and validation", rp1["saved"]["diagnosis"]["summary"] and rp1["saved"]["validation"]["invalid_rows"] == 2 and rp1["summary"]["period"]["label"] == "2026 T1")
    s, rec, _ = call("POST", "/recommendations", T, body={}); n = len(gem_calls())
    s, rp2, _ = call("GET", "/report", T); check("report picks up an already-generated briefing", rp2["narrative"] and rp2["narrative_model"] == "gemini-4-hydrogen" and len(gem_calls()) == n)
    s, e, _ = call("GET", "/report?year=2026&term=4", T); check("empty term -> empty flag", e["empty"] is True)
    s, e, _ = call("GET", "/report?year=2026&term=0", T); check("bad term -> 400", s == 400)

    # tenant isolation + auth
    s, rb, _ = call("GET", "/report", TB); check("other school: empty report, no leakage", rb["empty"] is True and "interventions" not in rb)
    bb, ct = mp(csvf(1, 0)); call("POST", "/ingest?year=2026&term=2", TB, raw=bb, ctype=ct)
    s, rb, _ = call("GET", "/report", TB); check("other school sees only its own data", rb["summary"]["learners"] == 1 and rb["interventions"] == [] and rb["school_name"] == "Other School" and (rb["saved"] or {}).get("diagnosis") is None)
    s, _, _ = call("GET", "/report"); check("report needs a token", s == 403)
    s, _, _ = call("GET", "/report", "school-bad"); check("report rejects a bad token", s == 403)

    # table missing: everything else must keep working
    fake("/_missing?t=sasams_term_reports"); start_api(); 
    body, ct = mp(csvf(3, 1)); s, r, _ = call("POST", "/ingest?year=2026&term=3", T, raw=body, ctype=ct); check("ingest survives missing term-report table", s == 200 and r["valid_count"] == 3)
    s, d, _ = call("POST", "/diagnose?year=2026&term=3", T, raw=body, ctype=ct); check("diagnose survives missing term-report table", s == 200 and d["diagnosis"])
    s, rp3, _ = call("GET", "/report?year=2026&term=2", T); check("report survives missing term-report table", s == 200 and rp3["saved"] is None and rp3["summary"]["learners"] == 3)
    fake("/_missing")
finally:
    stop_api()
    try: fk.send_signal(signal.SIGTERM)
    except Exception: pass
print("\nFAILED:" if fails else "\nALL PASSED", fails)
sys.exit(1 if fails else 0)
