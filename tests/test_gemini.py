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
def file_with(errors_n, ok_n=3):
    """ok_n good rows + errors_n bad-ID rows. Different sizes give different payloads (the AI answer is cached per payload)."""
    good = [f'{I[i]},"Smith, Jr",O\'Neil,10,Mathematics,60,0.9' for i in range(ok_n)]
    bad = ["1234567890123,Bad,Id,10,Mathematics,60,0.9"] * errors_n
    return "\r\n".join(good + bad).encode()

fk = subprocess.Popen([sys.executable, os.path.join(SP, "fake_pg.py")]); time.sleep(0.8)
try:
    start_api()
    s, h, _ = call("GET", "/health"); check("health reports diagnose_ai", s == 200 and h["diagnose_ai"] is True and h["ai_model"] is True)
    s, a, _ = call("POST", "/register", body={"school_name": "G School"}); T = a["token"]
    check("token keeps the readable school-<16 hex> format", len(T) == 7 + 16)

    # -- CSV parsing: BOM, quoted comma, apostrophe, blank line, CRLF
    body, ct = mp(b"\xef\xbb\xbf" + file_with(0, 3) + b"\r\n\r\n")
    s, r, _ = call("POST", "/ingest?year=2026&term=2", T, raw=body, ctype=ct)
    check("BOM + quoted commas + blank lines parse", s == 200 and r["valid_count"] == 3 and r["errors"] == [], str(r)[:300])
    stu = fake("/rest/v1/sasams_students?select=first_name,last_name")
    check("name with comma and apostrophe stored intact", {"first_name": "Smith, Jr", "last_name": "O'Neil"} in stu, str(stu)[:200])
    s, r, _ = call("POST", "/validate", T, raw=b"", ctype="text/csv"); check("empty upload rejected", s == 400)
    s, r, _ = call("POST", "/validate", T, raw=b"\xff\xfe\x00bad", ctype="text/csv"); check("non-UTF8 rejected cleanly", s == 400)
    s, r, _ = call("POST", "/validate", T, raw=file_with(1, 1), ctype="text/csv"); check("raw (non-multipart) CSV still works", s == 200 and r["valid_count"] == 1 and len(r["errors"]) == 1)

    # -- diagnose
    body, ct = mp(file_with(2, 3))
    s, d, _ = call("POST", "/diagnose", T, raw=body, ctype=ct)
    dg = d.get("diagnosis") or {}
    check("diagnose via Gemini", s == 200 and d["source"] == "gemini" and d["model"] == "gemini-4-hydrogen", str(d)[:300])
    check("diagnosis sanitised (markup stripped, bad severity fixed, bool coerced)", "<" not in json.dumps(dg) and dg["severity"] == "medium" and dg["manual_review_required"] is True, str(dg))
    check("validation counts returned", d["validation"] == {"total_rows_processed": 5, "valid_rows": 3, "invalid_rows": 2, "error_categories": {"identity_validation": 2}}, str(d["validation"]))
    gc = gem_calls(); sent = json.dumps(gc[-1]["body"])
    check("Gemini got the key in the header, not the URL", gc[-1]["key"] == "gem-key")
    check("NO names, IDs or row text sent to Gemini", all(x not in sent for x in ["Smith", "O'Neil", I[0], "1234567890123", "Bad"]), sent[:300])
    check("first style tried is the newest (responseFormat + thinkingLevel low)", "responseFormat" in gc[0]["body"]["generationConfig"] and gc[0]["body"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"} and gc[0]["body"]["generationConfig"]["responseFormat"]["text"]["schema"]["type"] == "object")
    n = len(gem_calls()); s, d2, _ = call("POST", "/diagnose", T, raw=body, ctype=ct)
    check("same payload cached: no second Gemini call", len(gem_calls()) == n and d2["source"] == "gemini")
    s, _, _ = call("POST", "/diagnose", None, raw=body, ctype=ct); check("diagnose requires a token", s == 403)
    s, _, _ = call("POST", "/diagnose", "school-bad", raw=body, ctype=ct); check("diagnose rejects a bad token", s == 403)
    s, e, _ = call("POST", "/diagnose", T, raw=b"\n\n", ctype="text/csv"); check("diagnose with no rows -> 400", s == 400)

    # -- request-style fallbacks (use new payload sizes to dodge the cache)
    for mode, size, expect_in_last in [("reject_new", 4, "responseSchema"), ("reject_new_legacy", 5, "Return ONLY one JSON object"), ("strict_last", 6, "Return ONLY one JSON object")]:
        start_api(); s, a, _ = call("POST", "/register", body={"school_name": "S-" + mode}); TT = a["token"]; gem_mode(mode)
        before = len(gem_calls()); body, ct = mp(file_with(size, 1)); s, d, _ = call("POST", "/diagnose", TT, raw=body, ctype=ct)
        used = len(gem_calls()) - before
        check(f"style fallback [{mode}] still gets an AI answer", s == 200 and d["source"] == "gemini", str(d)[:200])
        check(f"[{mode}] tried {'2' if mode == 'reject_new' else '3' if mode == 'reject_new_legacy' else '4'} styles", used == {"reject_new": 2, "reject_new_legacy": 3, "strict_last": 4}[mode], str(used))
        check(f"[{mode}] final request shape", expect_in_last in json.dumps(gem_calls()[-1]["body"]))
        before = len(gem_calls()); body, ct = mp(file_with(size + 3, 1)); s, d, _ = call("POST", "/diagnose", TT, raw=body, ctype=ct)
        check(f"[{mode}] working style remembered (1 call next time)", len(gem_calls()) - before == 1 and d["source"] == "gemini")
    gem_mode("fenced"); start_api(); s, a, _ = call("POST", "/register", body={"school_name": "Fenced"}); TF = a["token"]
    body, ct = mp(file_with(1, 1)); s, d, _ = call("POST", "/diagnose", TF, raw=body, ctype=ct); check("markdown-fenced JSON accepted", d["source"] == "gemini", str(d)[:200])

    # -- failures fall back to rules, never to an error page
    for mode, size in [("500", 1), ("bad_json", 2), ("empty", 3)]:
        gem_mode(mode); start_api(); s, a, _ = call("POST", "/register", body={"school_name": "F-" + mode}); TT = a["token"]
        body, ct = mp(file_with(size, 2)); s, d, _ = call("POST", "/diagnose", TT, raw=body, ctype=ct)
        check(f"Gemini [{mode}] -> rule-based diagnosis, HTTP 200", s == 200 and d["source"] == "rules" and "rule-based" in d["note"] and d["diagnosis"]["summary"], str(d)[:250])
    gem_mode("ok"); start_api(); s, a, _ = call("POST", "/register", body={"school_name": "Rate"}); TR = a["token"]
    srcs = []
    for k in range(5):
        body, ct = mp(file_with(k + 1, 2)); s, d, _ = call("POST", "/diagnose", TR, raw=body, ctype=ct); srcs.append(d["source"])
    check("per-school AI limit (3/hour) then rules", srcs == ["gemini"] * 3 + ["rules"] * 2, str(srcs))
    check("limit message shown", "limit" in d["note"].lower())

    # -- clean file: rule diagnosis is 'low'
    start_api(GEMINI_API_KEY=""); s, a, _ = call("POST", "/register", body={"school_name": "NoKey"}); TN = a["token"]
    s, h, _ = call("GET", "/health"); check("no key -> diagnose_ai false", h["diagnose_ai"] is False and h["ai_model"] is False)
    body, ct = mp(file_with(0, 3)); s, d, _ = call("POST", "/diagnose", TN, raw=body, ctype=ct)
    check("no key: rule diagnosis for a clean file is low/no review", s == 200 and d["source"] == "rules" and d["diagnosis"]["severity"] == "low" and d["diagnosis"]["manual_review_required"] is False, str(d)[:250])
    body, ct = mp(file_with(2, 2)); s, d, _ = call("POST", "/diagnose", TN, raw=body, ctype=ct)
    check("rule diagnosis flags ID problems for manual review", d["diagnosis"]["manual_review_required"] is True and d["diagnosis"]["severity"] == "high" and any("ID" in p for p in d["diagnosis"]["patterns"]), str(d["diagnosis"]))

    # -- recommendations use Gemini when no open model is configured
    gem_mode("ok"); start_api()
    s, a, _ = call("POST", "/register", body={"school_name": "Recs"}); TQ = a["token"]
    rows = "\n".join(f"{I[i]},A,B,10,Mathematics,{30 + i * 20},{0.6 + i * 0.1}" for i in range(4)).encode(); body, ct = mp(rows)
    call("POST", "/ingest?year=2026&term=2", TQ, raw=body, ctype=ct)
    n = len(gem_calls()); s, r, _ = call("POST", "/recommendations", TQ, body={})
    check("recommendations narrative via Gemini", s == 200 and r["narrative"] and r["model"] == "gemini-4-hydrogen" and "internal reasoning" not in r["narrative"], str(r)[:300])
    sent = json.dumps(gem_calls()[-1]["body"]); check("recommendation prompt has no learner data", I[0] not in sent and '"id_number"' not in sent)
    check("text mode does not ask for JSON schema", "responseFormat" not in gem_calls()[-1]["body"]["generationConfig"] and "responseSchema" not in gem_calls()[-1]["body"]["generationConfig"])

    # -- registration throttle + CORS
    start_api(REGISTER_PER_IP_PER_HOUR="3", REGISTER_GLOBAL_PER_HOUR="100")
    codes = [call("POST", "/register", body={"school_name": f"R{k}"})[0] for k in range(5)]
    check("registration throttled per IP", codes == [201, 201, 201, 429, 429], str(codes))
    start_api(ALLOWED_ORIGINS="http://good.example,http://also.example")
    def preflight(origin):
        r = urllib.request.Request(API + "/validate", method="OPTIONS", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
        with urllib.request.urlopen(r) as resp: return resp.status, resp.headers
    st, hd = preflight("http://also.example"); check("allowed origin echoed", st == 204 and hd["Access-Control-Allow-Origin"] == "http://also.example" and hd["Vary"] == "Origin")
    st, hd = preflight("http://evil.example"); check("unknown origin gets the first allowed origin, not itself", hd["Access-Control-Allow-Origin"] == "http://good.example")
    check("preflight allows required methods/headers", "PATCH" in hd["Access-Control-Allow-Methods"] and "X-Wrangler-Token" in hd["Access-Control-Allow-Headers"])
    start_api(); s, _, hd = call("GET", "/health"); check("default origin stays * (today's behaviour)", hd["Access-Control-Allow-Origin"] == "*")
    # -- earlier features untouched
    s, a, _ = call("POST", "/register", body={"school_name": "Old Faithful"}); s, l, _ = call("POST", "/login", a["token"])
    check("login still returns the registered school's name", s == 200 and l["school_name"] == "Old Faithful")
finally:
    stop_api()
    try: fk.send_signal(signal.SIGTERM)
    except Exception: pass
print("\nFAILED:" if fails else "\nALL PASSED", fails)
sys.exit(1 if fails else 0)
