"""Fake Supabase/PostgREST (generic tables) + fake Brevo on :9001, fake OpenAI-compatible LLM on :9002."""
import json, uuid, threading, datetime, sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
T = {"sasams_schools": [], "sasams_students": [], "sasams_student_history": [], "sasams_interventions": [], "sasams_term_reports": []}
UNIQUE = {"sasams_student_history": ("school_id", "id_number", "subject", "year", "term"), "sasams_term_reports": ("school_id", "year", "term")}
SEQ = {"n": 0}
SENT, LLM_CALLS = [], []
GEM = {"mode": "ok", "calls": []}
MISSING = set()   # tables to pretend don't exist
def cast(v):
    try: return int(v)
    except ValueError: return v
def filt(rows, q):
    for col, vals in q.items():
        if col in ("select", "limit", "offset", "order", "on_conflict"): continue
        v = vals[0]
        if v.startswith("eq."): rows = [r for r in rows if str(r.get(col)) == unquote(v[3:])]
        elif v.startswith("in."): s = set(v[4:-1].split(",")); rows = [r for r in rows if str(r.get(col)) in s]
        else: raise AssertionError("unsupported filter " + v)
    return rows
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _tbl(self, u):
        if not u.path.startswith("/rest/v1/"): return None
        t = u.path[len("/rest/v1/"):]
        if t in MISSING: self._send(404, {"code": "PGRST205", "message": "Could not find the table"}); return False
        return t if t in T else None
    def _auth_ok(self): return self.headers.get("apikey") == "k"
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/_sent": return self._send(200, SENT)
        if u.path == "/_llm": return self._send(200, LLM_CALLS)
        if u.path == "/_gemini_mode": GEM["mode"] = q.get("m", ["ok"])[0]; return self._send(200, GEM["mode"])
        if u.path == "/_gemini": return self._send(200, GEM["calls"])
        if u.path == "/_missing":
            MISSING.clear(); MISSING.update(q.get("t", [""])[0].split(",") if q.get("t") else []); return self._send(200, list(MISSING))
        t = self._tbl(u)
        if t is False: return
        if not t: return self._send(404, {})
        rows = filt(T[t], q)
        if "order" in q:
            col, _, d = q["order"][0].partition(".")
            rows = sorted(rows, key=lambda r: (r.get(col) is None, r.get(col)), reverse=(d == "desc"))
        if "created_at.desc" in q.get("order", [""])[0]: rows = list(reversed(T[t])) if False else rows
        off = int(q.get("offset", ["0"])[0]); lim = int(q.get("limit", ["1000"])[0])
        rows = rows[off:off + min(lim, 1000)]
        sel = q.get("select", ["*"])[0]
        if sel != "*": rows = [{k: r.get(k) for k in sel.split(",")} for r in rows]
        self._send(200, rows)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = json.loads(self.rfile.read(n) or b"{}"); u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/brevo/v3/smtp/email":
            if self.headers.get("api-key") != "test-key": return self._send(401, {"message": "bad api key"})
            SENT.append(body); return self._send(201, {"messageId": "<x@y>"})
        if u.path.startswith("/v1beta/models/") and u.path.endswith(":generateContent"):
            cfg = body.get("generationConfig", {}); mode = GEM["mode"]
            GEM["calls"].append({"key": self.headers.get("x-goog-api-key"), "model": u.path.split("/")[3].split(":")[0], "body": body})
            if self.headers.get("x-goog-api-key") != "gem-key": return self._send(403, {"error": "bad key"})
            if mode == "500": return self._send(500, {"error": "boom"})
            legacy = "responseSchema" in cfg or "responseMimeType" in cfg
            if mode in ("reject_new", "reject_new_legacy", "strict_last") and "responseFormat" in cfg: return self._send(400, {"error": "unknown field responseFormat"})
            if mode in ("reject_new_legacy", "strict_last") and legacy: return self._send(400, {"error": "unknown field responseSchema"})
            if mode == "strict_last" and ("thinkingConfig" in cfg or "systemInstruction" in body): return self._send(400, {"error": "unsupported"})
            wants_json = "responseFormat" in cfg or legacy or "Return ONLY one JSON object" in json.dumps(body)
            if mode == "empty": return self._send(200, {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "thinking...", "thought": True}]}}]})
            if wants_json:
                if mode == "bad_json": text = "sorry, no json today"
                else:
                    text = json.dumps({"summary": "Most errors are identity related. <script>alert(1)</script>", "severity": "BOGUS", "patterns": ["ID checksum failures <b>x</b>"], "recommendations": ["Verify IDs against SA-SAMS"], "manual_review_required": 1})
                    if mode == "fenced": text = "```json\n" + text + "\n```"
            else: text = "Overall the school is stable.\n- Strengthen Mathematics in Grade 10.\n- Follow up attendance."
            return self._send(200, {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "internal reasoning", "thought": True}, {"text": text}]}}]})
        if u.path == "/v1/chat/completions":
            LLM_CALLS.append({"auth": self.headers.get("Authorization"), "body": body})
            return self._send(200, {"choices": [{"message": {"content": "<think>x</think>Overall the school is stable.\n- Strengthen Mathematics in Grade 10.\n- Follow up attendance."}}]})
        t = self._tbl(u)
        if t is False: return
        if not t: return self._send(404, {})
        if not self._auth_ok(): return self._send(401, {"message": "bad key"})
        rows = body if isinstance(body, list) else [body]
        out = []
        prefer = self.headers.get("Prefer", "")
        for r in rows:
            r = dict(r)
            if t == "sasams_schools": r["id"] = str(uuid.uuid4()); r["created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            if t in UNIQUE:
                key = UNIQUE[t]; ex = next((x for x in T[t] if all(x.get(k) == r.get(k) for k in key)), None)
                if ex is not None:
                    if "merge-duplicates" not in prefer: return self._send(409, {"message": "duplicate"})
                    ex.update(r); out.append(ex); continue
                assert q.get("on_conflict") or "merge" not in prefer
            if t in ("sasams_student_history", "sasams_interventions"): SEQ["n"] += 1; r["id"] = SEQ["n"]
            if t == "sasams_interventions":
                r.setdefault("created_at", datetime.datetime.now(datetime.timezone.utc).isoformat())
            T[t].append(r); out.append(r)
        self._send(201, out if "representation" in prefer else [])
    def do_PATCH(self):
        n = int(self.headers.get("Content-Length", 0)); body = json.loads(self.rfile.read(n) or b"{}"); u = urlparse(self.path); q = parse_qs(u.query)
        t = self._tbl(u)
        if t is False: return
        rows = filt(T[t], q)
        for r in rows: r.update(body)
        self._send(200, rows)
    def do_DELETE(self):
        u = urlparse(self.path); q = parse_qs(u.query); t = self._tbl(u)
        if t is False: return
        rows = filt(T[t], q)
        for r in rows: T[t].remove(r)
        self._send(200, rows)
class S(ThreadingHTTPServer): daemon_threads = True
for port in (9001, 9002):
    threading.Thread(target=S(("127.0.0.1", port), H).serve_forever, daemon=True).start()
threading.Event().wait()
