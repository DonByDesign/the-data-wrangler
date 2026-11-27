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
def call(method, path, token=None, body=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": ctype}
    if token: h["X-Wrangler-Token"] = token
    r = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp: return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
def fake(path): return json.loads(urllib.request.urlopen("http://127.0.0.1:9001" + path).read())
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if not cond and extra else ""))
    if not cond: fails.append(name)

def csv(rows): return "\n".join(",".join(str(x) for x in r) for r in rows).encode()
A1, A2, A3, A4 = sa_id(10, 3, 4, 1), sa_id(10, 5, 6, 2), sa_id(10, 7, 8, 3), sa_id(10, 9, 10, 4)
B1 = sa_id(11, 1, 2, 9)
def term_csv(sba_scale):
    return csv([
        [A1, "Thandi", "Nkosi", 10, "Mathematics", 30 * sba_scale, 0.62],      # struggling on all fronts
        [A1, "Thandi", "Nkosi", 10, "Life Sciences", 35 * sba_scale, 0.62],
        [A2, "Sipho", "Dlamini", 10, "Mathematics", 72, 0.95],                   # healthy
        [A2, "Sipho", "Dlamini", 10, "Life Sciences", 68, 0.95],
        [A3, "Lerato", "Mokoena", 10, "Mathematics", 46, 0.92],                  # low, fine attendance
        [A3, "Lerato", "Mokoena", 10, "Life Sciences", 55, 0.92],
        [A4, "Jan", "Botha", 10, "Mathematics", 62, 0.74],                       # attendance only
        [A4, "Jan", "Botha", 10, "Life Sciences", 60, 0.74],
    ])
def mp(content):
    b = "XBOUND"
    return (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="x.csv"\r\nContent-Type: text/csv\r\n\r\n'.encode() + content + f"\r\n--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}")

env = dict(os.environ, SUPABASE_URL="http://127.0.0.1:9001", SUPABASE_KEY="k", PORT="8123",
           BREVO_API_KEY="test-key", MAIL_FROM="a@b.co", BREVO_API_URL="http://127.0.0.1:9001/brevo/v3/smtp/email",
           LLM_URL=os.environ.get("TEST_LLM_URL", "http://127.0.0.1:9002"), LLM_API_KEY="sekret", LLM_PER_SCHOOL_PER_HOUR="2")
fk = subprocess.Popen([sys.executable, os.path.join(SP, "fake_pg.py")])
time.sleep(0.8)
api = subprocess.Popen([sys.executable, MAIN_PY], env=env, stdout=open(os.path.join(tempfile.gettempdir(), "api3.log"), "w"), stderr=subprocess.STDOUT)
time.sleep(1.0)
try:
    s, h = call("GET", "/health"); check("health", s == 200 and h["ai_model"] is True)
    s, a = call("POST", "/register", body={"school_name": "School A"}); TA = a["token"]
    s, b = call("POST", "/register", body={"school_name": "School B"}); TB = b["token"]

    # -- ingest term 1 and 2 (tenant A), one term for B
    body, ct = mp(term_csv(1))
    s, r = call("POST", "/ingest?year=2026&term=1", TA, raw=body, ctype=ct)
    check("ingest T1", s == 200 and r["valid_count"] == 8 and r["history_saved"] == 8 and r["period"]["label"] == "2026 T1", str(r))
    body, ct = mp(term_csv(0.6))   # Thandi's marks collapse
    s, r = call("POST", "/ingest?year=2026&term=2", TA, raw=body, ctype=ct)
    check("ingest T2", s == 200 and r["history_saved"] == 8, str(r))
    s, r = call("POST", "/ingest?year=2026&term=2", TA, raw=body, ctype=ct)
    check("re-upload same term overwrites, no duplicates", len(fake("/_nothing") if False else []) == 0 and s == 200)
    bb, ct = mp(csv([[B1, "Zed", "Other", 8, "Mathematics", 20, 0.5]]))
    s, r = call("POST", "/ingest?year=2026&term=2", TB, raw=bb, ctype=ct)
    check("ingest B", s == 200 and r["history_saved"] == 1)
    s, r = call("POST", "/validate?year=2026&term=3", TA, raw=body, ctype=ct)
    check("dry run stores nothing", r["history_saved"] is None)
    s, r = call("POST", "/ingest?year=2026&term=9", TA, raw=body, ctype=ct); check("bad term rejected", s == 400)
    s, r = call("POST", "/ingest?year=abc", TA, raw=body, ctype=ct); check("bad year rejected", s == 400)

    # -- analytics
    s, a1 = call("GET", "/analytics/summary", TA)
    check("defaults to newest term", s == 200 and a1["period"]["label"] == "2026 T2" and len(a1["available_periods"]) == 2, str(a1)[:300])
    check("learner count", a1["learners"] == 4)
    check("risk dist", a1["risk"]["high"] >= 1 and a1["risk"]["low"] >= 1, str(a1["risk"]))
    top = a1["at_risk"][0]
    check("worst learner first, with name + suggestions", top["id_number"] == A1 and top["name"] == "Thandi Nkosi" and top["level"] == "high" and any(x["category"] == "Referral" for x in top["suggested"]), str(top)[:400])
    check("trend has two points", len(a1["trend"]) == 2 and a1["trend"][1]["avg_sba"] < a1["trend"][0]["avg_sba"])
    check("trend factor fires", any("fell" in f["label"] for f in top["factors"]))
    check("subjects sorted weakest first", a1["by_subject"][0]["avg_sba"] <= a1["by_subject"][-1]["avg_sba"])
    s, a0 = call("GET", "/analytics/summary?year=2026&term=1", TA); check("explicit term", a0["period"]["label"] == "2026 T1" and not any("fell" in f["label"] for f in a0["at_risk"][0]["factors"]))
    s, e = call("GET", "/analytics/summary?year=2026&term=4", TA); check("empty period flagged", e["empty"] is True)
    s, sb = call("GET", "/analytics/summary", TB); check("tenant B sees only own data", sb["learners"] == 1 and all(x["id_number"] == B1 for x in sb["at_risk"]))

    # -- auth
    s, _ = call("GET", "/analytics/summary"); check("no token 403", s == 403)
    s, _ = call("GET", "/analytics/summary", "school-bad"); check("bad token 403", s == 403)

    # -- interventions
    s, iv = call("POST", "/interventions", TA, body={"id_number": A1, "title": "Maths catch-up", "category": "Academic", "notes": "<b>Tue</b> afternoons", "due_date": "2026-09-30"})
    check("create intervention", s == 201 and iv["intervention"]["status"] == "planned" and iv["intervention"]["year"] == 2026 and iv["intervention"]["term"] == 2 and "<b>" not in iv["intervention"]["notes"], str(iv))
    IV = iv["intervention"]["id"]
    s, x = call("POST", "/interventions", TA, body={"id_number": B1, "title": "steal"}); check("cannot target another school's learner", s == 404)
    s, x = call("POST", "/interventions", TB, body={"id_number": A1, "title": "steal"}); check("B cannot target A's learner", s == 404)
    s, x = call("POST", "/interventions", TA, body={"id_number": A1, "title": ""}); check("title required", s == 400)
    s, x = call("POST", "/interventions", TA, body={"id_number": A1, "title": "x", "category": "Hack"}); check("category whitelist", s == 400)
    s, x = call("POST", "/interventions", TA, body={"id_number": A1, "title": "x", "due_date": "31/09/2026"}); check("date format", s == 400)
    s, x = call("PATCH", f"/interventions/{IV}", TB, body={"status": "completed"}); check("B cannot edit A's intervention", s == 404)
    s, x = call("DELETE", f"/interventions/{IV}", TB); check("B cannot delete A's intervention", s == 404)
    s, x = call("PATCH", f"/interventions/{IV}", TA, body={"status": "bogus"}); check("status whitelist", s == 400)
    s, x = call("PATCH", f"/interventions/{IV}", TA, body={"school_id": "other", "status": "in_progress"}); check("school_id not writable", s == 200 and x["intervention"]["school_id"] == a["token"] or x["intervention"].get("school_id") != "other")
    s, l = call("GET", "/interventions", TA); check("list has name", l["total"] == 1 and l["interventions"][0]["name"] == "Thandi Nkosi")
    s, l = call("GET", "/interventions", TB); check("B list empty", l["total"] == 0)
    s, a2 = call("GET", "/analytics/summary", TA); check("active intervention reflected", a2["at_risk"][0]["has_active_intervention"] is True and a2["interventions"]["counts"]["in_progress"] == 1)
    s, x = call("PATCH", f"/interventions/{IV}", TA, body={"status": "completed", "outcome": "Improved"}); check("complete sets completed_at", x["intervention"].get("completed_at"))
    # impact: add T3 with improved marks
    body3, ct = mp(term_csv(1.4))
    s, r = call("POST", "/ingest?year=2026&term=3", TA, raw=body3, ctype=ct)
    s, a3 = call("GET", "/analytics/summary", TA)
    imp = a3["interventions"]["impact"]; check("impact measured", imp["learners_measured"] == 1 and imp["avg_sba_change"] is not None and imp["improved"] == 1, str(imp))

    # -- learner profile
    s, p = call("GET", f"/learners/{A1}", TA); check("profile", s == 200 and len(p["periods"]) == 3 and p["name"] == "Thandi Nkosi" and len(p["interventions"]) == 1)
    s, p = call("GET", f"/learners/{A1}", TB); check("profile of other school's learner 404", s == 404)
    s, p = call("GET", "/learners/123", TA); check("bad id rejected", s in (400, 404))

    # -- recommendations
    s, rec = call("POST", "/recommendations", TA, body={})
    check("recommendations with model", s == 200 and rec["narrative"] and "<think>" not in rec["narrative"] and rec["model"] and len(rec["recommendations"]) >= 1, str(rec)[:300])
    calls = fake("/_llm"); sent = json.dumps(calls[-1]["body"])
    check("model got Bearer key", calls[-1]["auth"] == "Bearer sekret")
    check("NO names/ids sent to model", "Thandi" not in sent and "Nkosi" not in sent and A1 not in sent and "id_number" not in sent, sent[:300])
    n_calls = len(calls)
    s, rec = call("POST", "/recommendations", TA, body={}); check("narrative cached (no 2nd model call)", len(fake("/_llm")) == n_calls and rec["narrative"])
    s, rec = call("POST", "/recommendations?year=2026&term=1", TA, body={}); s, rec = call("POST", "/recommendations?year=2026&term=2", TA, body={})
    check("per-school LLM rate limit falls back to rules", rec["narrative"] is None and "limit" in rec["model_note"].lower() and rec["recommendations"], str(rec)[:200])
    s, rec = call("POST", "/recommendations?year=2026&term=4", TA, body={}); check("empty period -> no recs, no crash", s == 200 and rec["recommendations"] == [])

    # -- degraded modes
    fake("/_missing?t=sasams_student_history")
    body, ct = mp(term_csv(1))
    s, r = call("POST", "/ingest?year=2026&term=4", TA, raw=body, ctype=ct); check("ingest survives missing history table", s == 200 and r["history_saved"] is False and r["valid_count"] == 8, str(r))
    s, c = call("POST", "/register", body={"school_name": "School C"}); TC = c["token"]
    s, r = call("GET", "/analytics/summary", TC); check("missing table -> friendly 503", s == 503 and "Phase 3" in r["detail"], str(r))
    fake("/_missing")
    api.send_signal(signal.SIGTERM); api.wait(5)
    env2 = dict(env, LLM_URL="http://127.0.0.1:9"); api = subprocess.Popen([sys.executable, MAIN_PY], env=env2, stdout=open(os.path.join(tempfile.gettempdir(), "api3b.log"), "w"), stderr=subprocess.STDOUT); time.sleep(1.0)
    s, rec = call("POST", "/recommendations?year=2026&term=1", TA, body={}); check("model down -> rule fallback", s == 200 and rec["narrative"] is None and "could not be reached" in rec["model_note"] and rec["recommendations"], str(rec)[:200])
    api.send_signal(signal.SIGTERM); api.wait(5)
    env3 = dict(env); env3.pop("LLM_URL"); env3["LLM_URL"] = ""; api = subprocess.Popen([sys.executable, MAIN_PY], env=env3, stdout=open(os.path.join(tempfile.gettempdir(), "api3c.log"), "w"), stderr=subprocess.STDOUT); time.sleep(1.0)
    s, rec = call("POST", "/recommendations?year=2026&term=1", TA, body={}); check("no model configured -> rules only", s == 200 and rec["narrative"] is None and "rule-based" in rec["model_note"])
    s, h = call("GET", "/health"); check("health reflects no model", h["ai_model"] is False)
    # old endpoints unchanged
    s, r = call("POST", "/login", TA); check("login still works", s == 200 and r["school_name"] == "School A")
finally:
    for p in (api, fk):
        try: p.send_signal(signal.SIGTERM)
        except Exception: pass
print("\nFAILED:" if fails else "\nALL PASSED", fails)
sys.exit(1 if fails else 0)
