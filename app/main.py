import os
import re
import json
import csv
import io
import logging
from collections import Counter
import secrets
import time
import threading
import html as html_lib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
LOGGER = logging.getLogger("data-wrangler")

def log(message):
    LOGGER.info(message)

# 1. Target Endpoint Config (Aggressive cleaning to fix DNS errors)
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().strip("'\"").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip().strip("'\"")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("CRITICAL: Missing environment variables. Server refusing to start.")

# 2. Email Config (optional).
# Render's free tier blocks SMTP ports (25/465/587), so mail goes out through
# Brevo's HTTPS API instead. If these two variables are missing, the app still
# works; it just can't email tokens.
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip().strip("'\"")
MAIL_FROM = os.getenv("MAIL_FROM", "").strip().strip("'\"")          # must be a verified sender in Brevo
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "The Data Wrangler").strip()
APP_URL = os.getenv("APP_URL", "").strip().rstrip("/")                # optional: link shown in the email
BREVO_API_URL = os.getenv("BREVO_API_URL", "https://api.brevo.com/v3/smtp/email")
EMAIL_ENABLED = bool(BREVO_API_KEY and MAIL_FROM)
EMAIL_PER_ADDRESS_PER_HOUR = 3
EMAIL_GLOBAL_PER_HOUR = int(os.getenv("EMAIL_GLOBAL_PER_HOUR", "60"))  # keeps you inside the free daily quota

# 3. AI model (optional). Any OpenAI-compatible server works (llama.cpp, Ollama,
# vLLM, a hosted API). If LLM_URL is empty the app uses its built-in rule engine only.
LLM_URL = os.getenv("LLM_URL", "").strip().strip("'\"").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3-4b-instruct").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip().strip("'\"")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "150"))   # generous: a scale-to-zero model needs a cold start
LLM_PER_SCHOOL_PER_HOUR = int(os.getenv("LLM_PER_SCHOOL_PER_HOUR", "6"))

# 4. Gemini (hosted AI). Optional: without a key the app uses rule-based diagnosis and recommendations.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip().strip("'\"")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-4-hydrogen").strip()
GEMINI_API_BASE = os.getenv("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "45"))
GEMINI_RETRIES = int(os.getenv("GEMINI_RETRIES", "2"))                 # extra tries when Google says "busy" (503/429)
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()   # optional second model if the first stays busy
GEMINI_THINKING = os.getenv("GEMINI_THINKING_LEVEL", "low").strip()   # low keeps answers fast and cheap
GEMINI_MAX_TOKENS = int(os.getenv("GEMINI_MAX_TOKENS", "4096"))       # thinking tokens count against this
GEMINI_PER_SCHOOL_PER_HOUR = int(os.getenv("GEMINI_PER_SCHOOL_PER_HOUR", "20"))
GEMINI_GLOBAL_PER_HOUR = int(os.getenv("GEMINI_GLOBAL_PER_HOUR", "300"))   # protects your key/bill: anyone can register a school
REGISTER_PER_IP_PER_HOUR = int(os.getenv("REGISTER_PER_IP_PER_HOUR", "10"))
REGISTER_GLOBAL_PER_HOUR = int(os.getenv("REGISTER_GLOBAL_PER_HOUR", "60"))
# Comma-separated list of allowed website origins. "*" (default) keeps today's behaviour; set it to
# your static site's address (https://data-wrangler-preflight.onrender.com) to lock the API to it.
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").strip()

MAX_UPLOAD_SIZE = 5 * 1024 * 1024

VALID_SUBJECTS = [
    "Mathematics", "Mathematical Literacy", "Technical Mathematics",
    "Physical Sciences", "Life Sciences", "Agricultural Sciences",
    "Accounting", "Business Studies", "Economics",
    "Geography", "History", "Tourism",
    "English Home Language", "English First Additional Language",
    "Afrikaans Home Language", "Afrikaans First Additional Language",
    "isiZulu Home Language", "isiZulu First Additional Language",
    "isiXhosa Home Language", "isiXhosa First Additional Language",
    "Life Orientation", "Computer Applications Technology", "Information Technology"
]

def sanitize_string(v: str) -> str:
    # Strip markup and trim, but keep legitimate punctuation (D'Souza, O'Neil).
    return re.sub(r"<[^>]*>", "", str(v)).strip()

def validate_sa_id(id_str: str):
    if not re.match(r"^\d{13}$", id_str):
        return False, "Must be exactly 13 digits."
    yy, mm, dd = id_str[0:2], id_str[2:4], id_str[4:6]
    current_year = datetime.now().year % 100
    yy_int = int(yy)
    year = 1900 + yy_int if yy_int > current_year else 2000 + yy_int
    try:
        datetime(int(year), int(mm), int(dd))
    except ValueError:
        return False, "Invalid birth date."
    odd_digits = id_str[0:12:2]
    even_digits = id_str[1:12:2]
    odd_sum = sum(int(d) for d in odd_digits)
    even_sum = 0
    for d in even_digits:
        double = int(d) * 2
        even_sum += double if double < 10 else double - 9
    total = odd_sum + even_sum
    check_digit = (10 - (total % 10)) % 10
    if check_digit != int(id_str[12]):
        return False, "Invalid ID checksum (Luhn)."
    return True, "Valid"

def validate_row(fields, index: int):
    fields = [f.strip() for f in fields]

    if len(fields) != 7:
        return None, f"Row {index}: Expected 7 columns, got {len(fields)}"

    id_num, first, last, grade, subject, sba, att = fields

    is_valid_id, id_error = validate_sa_id(id_num)
    if not is_valid_id:
        return None, f"Row {index}: {id_error}"

    if subject not in VALID_SUBJECTS:
        return None, f"Row {index}: Invalid subject '{subject}'."

    try:
        grade_int = int(grade)
        if not (1 <= grade_int <= 12):
            return None, f"Row {index}: Grade must be 1-12."

        sba_float = float(sba) if sba else None
        if sba_float is not None and not (0.0 <= sba_float <= 100.0):
            return None, f"Row {index}: SBA mark 0-100."

        att_float = float(att)
        if not (0.0 <= att_float <= 1.0):
            return None, f"Row {index}: Attendance 0.0-1.0."
    except ValueError:
        return None, f"Row {index}: Numeric parsing failed."

    return {
        "id_number": id_num,
        "first_name": sanitize_string(first),
        "last_name": sanitize_string(last),
        "grade": grade_int,
        "subject": subject,
        "sba_mark": sba_float,
        "attendance_rate": att_float
    }, None

def parse_csv_upload(body: bytes, content_type: str) -> str:
    """Raw CSV or multipart upload -> text. utf-8-sig drops the BOM that Excel adds."""
    if "multipart/form-data" not in content_type.lower():
        return body.decode("utf-8-sig")
    from email.parser import BytesParser
    from email.policy import default
    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + content_type.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    for part in message.iter_parts():
        if part.get_filename():
            return (part.get_payload(decode=True) or b"").decode("utf-8-sig")
    raise ValueError("No file part found in the upload.")

# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")

def clean_email(value) -> str:
    """Returns a lower-cased email, '' if none given, or raises ValueError."""
    email = str(value or "").strip().lower()
    if not email:
        return ""
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise ValueError("Please enter a valid email address.")
    return email

def mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:1]}***@{domain}"

_rate_lock = threading.Lock()
_rate_hits = {}

def rate_ok(key: str, limit: int, window: int = 3600) -> bool:
    """Simple in-memory limiter: at most `limit` hits per `window` seconds for `key`."""
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(key, []) if now - t < window]
        if len(hits) >= limit:
            _rate_hits[key] = hits
            return False
        hits.append(now)
        _rate_hits[key] = hits
        if len(_rate_hits) > 5000:  # keep memory bounded
            for k in [k for k, v in _rate_hits.items() if not v or now - v[-1] >= window]:
                _rate_hits.pop(k, None)
        return True

def build_token_email(entries):
    esc = html_lib.escape
    blocks = "".join(
        '<p style="margin:18px 0 4px;color:#555">' + esc(name) + '</p>'
        '<p style="font-family:Consolas,Menlo,monospace;font-size:18px;font-weight:bold;'
        'background:#f2f2f7;padding:12px 14px;border-radius:8px;word-break:break-all;margin:0">' + esc(tok) + '</p>'
        for name, tok in entries
    )
    link = ('<p style="margin-top:20px"><a href="' + esc(APP_URL) + '">Open The Data Wrangler</a></p>') if APP_URL else ""
    return (
        '<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:480px;margin:0 auto;color:#1d1d1f">'
        '<h2 style="margin-bottom:6px">Your school token</h2>'
        '<p>Use this token to log in to The Data Wrangler. Keep it private: anyone who has it can open your school workspace.</p>'
        + blocks + link +
        '<p style="color:#777;font-size:12px;margin-top:28px">If you did not ask for this email, you can safely ignore it.</p>'
        '</div>'
    )

def send_email(to_email: str, subject: str, html_body: str) -> bool:
    if not EMAIL_ENABLED:
        return False
    payload = {
        "sender": {"name": MAIL_FROM_NAME, "email": MAIL_FROM},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_body,
    }
    req = urllib.request.Request(
        BREVO_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-key": BREVO_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "DataWrangler/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            response.read()
        return True
    except urllib.error.HTTPError as e:
        log(f"DEBUG: Email provider rejected the request: HTTP {e.code} {e.read().decode('utf-8', 'ignore')[:300]}")
    except Exception as e:
        log(f"DEBUG: Email send failed: {e}")
    return False

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def supabase_get(path_and_query: str):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path_and_query}",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))

def find_school_by_token(token: str):
    """Returns {"id", "school_name"} or None. Raises on database errors."""
    safe = urllib.parse.quote(token, safe="")
    rows = supabase_get(f"sasams_schools?api_token=eq.{safe}&select=id,school_name&limit=1")
    return rows[0] if rows else None

def supabase_request(method: str, path_and_query: str, body=None, prefer: str = "", timeout: int = 20):
    """Generic PostgREST call. Returns parsed JSON (or None for an empty body)."""
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{path_and_query}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else None

def supabase_get_all(path_and_query: str, page: int = 1000, cap: int = 60000):
    """Reads every page of a query (PostgREST returns at most ~1000 rows per call)."""
    sep = "&" if "?" in path_and_query else "?"
    out, offset = [], 0
    while offset < cap:
        rows = supabase_request("GET", f"{path_and_query}{sep}limit={page}&offset={offset}") or []
        out.extend(rows)
        if len(rows) < page:
            break
        offset += page
    return out

def q(value) -> str:
    """URL-quote a value for use inside a PostgREST filter."""
    return urllib.parse.quote(str(value), safe="")

# Short-lived per-school cache so one page load (summary + interventions + ...)
# reads the database once. Dropped whenever that school's data changes.
_cache_lock = threading.Lock()
_cache = {}
CACHE_TTL = 30

def cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    return None

def cache_put(key, value):
    with _cache_lock:
        if len(_cache) > 40:
            _cache.clear()
        _cache[key] = (time.time(), value)

def cache_drop(school_id):
    with _cache_lock:
        for k in [k for k in _cache if k[0] == str(school_id)]:
            _cache.pop(k, None)

def load_history(school_id):
    key = (str(school_id), "history")
    rows = cache_get(key)
    if rows is None:
        rows = supabase_get_all(
            f"sasams_student_history?school_id=eq.{q(school_id)}"
            "&select=id_number,subject,grade,sba_mark,attendance_rate,year,term&order=id")
        cache_put(key, rows)
    return rows

def load_names(school_id):
    key = (str(school_id), "names")
    names = cache_get(key)
    if names is None:
        rows = supabase_get_all(
            f"sasams_students?school_id=eq.{q(school_id)}&select=id_number,first_name,last_name&order=id")
        names = {}
        for r in rows:
            names[r["id_number"]] = f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip()
        cache_put(key, names)
    return names

def load_interventions(school_id):
    key = (str(school_id), "interventions")
    rows = cache_get(key)
    if rows is None:
        rows = supabase_get_all(
            f"sasams_interventions?school_id=eq.{q(school_id)}&select=*&order=id.desc")
        cache_put(key, rows)
    return rows

def save_history(school_id, records, year: int, term: int):
    """Stores this upload as a snapshot for (year, term). Re-uploading a term overwrites it."""
    dedup = {}
    for r in records:
        dedup[(r["id_number"], r["subject"])] = {
            "school_id": school_id, "id_number": r["id_number"], "subject": r["subject"],
            "grade": r["grade"], "sba_mark": r["sba_mark"], "attendance_rate": r["attendance_rate"],
            "year": year, "term": term,
        }
    rows = list(dedup.values())
    for i in range(0, len(rows), 500):
        supabase_request(
            "POST", "sasams_student_history?on_conflict=school_id,id_number,subject,year,term",
            rows[i:i + 500], prefer="resolution=merge-duplicates,return=minimal")
    cache_drop(school_id)
    return len(rows)

def save_term_report(school_id, year: int, term: int, **cols):
    """Remembers per-term validation counts and the diagnosis, for the School Report.
    Merge-upsert: each writer only updates the columns it sends."""
    row = {"school_id": school_id, "year": year, "term": term, "updated_at": now_iso(), **cols}
    supabase_request("POST", "sasams_term_reports?on_conflict=school_id,year,term", [row],
                     prefer="resolution=merge-duplicates,return=minimal")

def load_term_report(school_id, year: int, term: int):
    rows = supabase_request("GET", f"sasams_term_reports?school_id=eq.{q(school_id)}&year=eq.{int(year)}&term=eq.{int(term)}&select=*&limit=1")
    return rows[0] if rows else None

# ---------------------------------------------------------------------------
# Terms
# ---------------------------------------------------------------------------
def qget(qs, key):
    v = qs.get(key)
    return v[0].strip() if v else ""

def default_period(now=None):
    now = now or datetime.now()
    return now.year, min(4, (now.month - 1) // 3 + 1)

def parse_period(qs, fallback=None):
    """Reads ?year=&term=. Missing values fall back to `fallback` (or the calendar term)."""
    fy, ft = fallback or default_period()
    try:
        year = int(qget(qs, "year") or fy)
        term = int(qget(qs, "term") or ft)
    except ValueError:
        raise ValueError("Year and term must be numbers.")
    if not (2000 <= year <= 2100) or term not in (1, 2, 3, 4):
        raise ValueError("Term must be 1 to 4 and year between 2000 and 2100.")
    return year, term

def prev_period(year: int, term: int):
    return (year, term - 1) if term > 1 else (year - 1, 4)

def period_label(year: int, term: int) -> str:
    return f"{year} T{term}"

# ---------------------------------------------------------------------------
# Risk engine (rule-based, explainable, no AI involved).
# The SA-SAMS export has no behaviour/discipline data, so risk is based on the
# two signals the data really contains: marks and attendance, plus their trend.
# ---------------------------------------------------------------------------
PASS_MARK = 50.0
HIGH_RISK_SCORE = 50
MEDIUM_RISK_SCORE = 25

def num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None

def _avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None

def learner_risk(rows, prev_rows=None):
    marks = [num(r.get("sba_mark")) for r in rows]
    atts = [num(r.get("attendance_rate")) for r in rows]
    avg_sba, att = _avg(marks), _avg(atts)
    failing = sorted({r["subject"] for r in rows if num(r.get("sba_mark")) is not None and num(r["sba_mark"]) < PASS_MARK})
    factors = []

    def add(category, label, points):
        factors.append({"category": category, "label": label, "points": points})

    if avg_sba is not None:
        if avg_sba < 40:
            add("Academic", f"Average SBA {avg_sba:.0f}% is below 40%", 35)
        elif avg_sba < PASS_MARK:
            add("Academic", f"Average SBA {avg_sba:.0f}% is below the 50% pass mark", 20)
        if len(failing) >= 3:
            add("Academic", f"Below 50% in {len(failing)} subjects", 10)
    if att is not None:
        if att < 0.70:
            add("Attendance", f"Attendance {att * 100:.0f}% is below 70%", 35)
        elif att < 0.80:
            add("Attendance", f"Attendance {att * 100:.0f}% is below 80%", 20)
        elif att < 0.90:
            add("Attendance", f"Attendance {att * 100:.0f}% is below 90%", 8)

    if prev_rows:
        p_sba = _avg([num(r.get("sba_mark")) for r in prev_rows])
        p_att = _avg([num(r.get("attendance_rate")) for r in prev_rows])
        if avg_sba is not None and p_sba is not None:
            drop = p_sba - avg_sba
            if drop >= 15:
                add("Academic", f"Marks fell {drop:.0f} points since last term", 25)
            elif drop >= 8:
                add("Academic", f"Marks fell {drop:.0f} points since last term", 12)
        if att is not None and p_att is not None and p_att - att >= 0.10:
            add("Attendance", f"Attendance fell {(p_att - att) * 100:.0f} points since last term", 10)

    score = min(100, sum(f["points"] for f in factors))
    level = "high" if score >= HIGH_RISK_SCORE else "medium" if score >= MEDIUM_RISK_SCORE else "low"
    return {
        "score": score, "level": level,
        "avg_sba": None if avg_sba is None else round(avg_sba, 1),
        "attendance_pct": None if att is None else round(att * 100, 1),
        "failing_subjects": failing, "factors": factors,
        "categories": sorted({f["category"] for f in factors}),
    }

def suggest_interventions(risk):
    """Maps a learner's risk factors to concrete, SA-school-appropriate next steps."""
    out, cats = [], set(risk["categories"])
    if "Academic" in cats:
        subj = ", ".join(risk["failing_subjects"][:3]) or "the weakest subjects"
        out.append({"category": "Academic", "title": "Academic support plan",
                    "notes": f"Catch-up lessons and a revised SBA plan for {subj}."})
    if "Attendance" in cats:
        out.append({"category": "Attendance", "title": "Attendance follow-up",
                    "notes": "Contact parent/guardian, find the reason for absences and agree an attendance plan."})
    if risk["level"] == "high" and len(cats) >= 2:
        out.append({"category": "Referral", "title": "Refer to School-Based Support Team",
                    "notes": "Marks and attendance are both a concern; discuss at the next SBST meeting."})
    return out

def group_by_learner(rows):
    out = {}
    for r in rows:
        out.setdefault(r["id_number"], []).append(r)
    return out

def periods_in(history):
    return sorted({(int(r["year"]), int(r["term"])) for r in history})

ACTIVE_STATUSES = ("planned", "in_progress")

def intervention_impact(history, interventions):
    """Compares each supported learner's average mark before/after their first intervention.
    This shows movement, not proof that the intervention caused it."""
    periods = periods_in(history)
    if len(periods) < 2:
        return {"learners_measured": 0, "avg_sba_change": None, "improved": 0}
    latest = periods[-1]
    first_by_learner = {}
    for iv in interventions:
        if iv.get("status") == "cancelled" or iv.get("year") is None or iv.get("term") is None:
            continue
        p = (int(iv["year"]), int(iv["term"]))
        cur = first_by_learner.get(iv["id_number"])
        if cur is None or p < cur:
            first_by_learner[iv["id_number"]] = p
    by_learner_period = {}
    for r in history:
        by_learner_period.setdefault((r["id_number"], int(r["year"]), int(r["term"])), []).append(num(r.get("sba_mark")))
    deltas = []
    for lid, p in first_by_learner.items():
        if p >= latest:
            continue
        base = _avg(by_learner_period.get((lid, p[0], p[1]), []))
        now = _avg(by_learner_period.get((lid, latest[0], latest[1]), []))
        if base is not None and now is not None:
            deltas.append(now - base)
    return {
        "learners_measured": len(deltas),
        "avg_sba_change": round(sum(deltas) / len(deltas), 1) if deltas else None,
        "improved": sum(1 for d in deltas if d > 0),
        "baseline_to": period_label(*latest),
    }

def build_summary(history, year, term, names=None, interventions=None):
    names = names or {}
    interventions = interventions or []
    periods = periods_in(history)
    rows = [r for r in history if int(r["year"]) == year and int(r["term"]) == term]
    py, pt = prev_period(year, term)
    prev_rows = [r for r in history if int(r["year"]) == py and int(r["term"]) == pt]
    prev_by = group_by_learner(prev_rows)
    by_learner = group_by_learner(rows)

    risks = {lid: learner_risk(lr, prev_by.get(lid)) for lid, lr in by_learner.items()}
    dist = {"high": 0, "medium": 0, "low": 0}
    for r in risks.values():
        dist[r["level"]] += 1

    active_ids = {iv["id_number"] for iv in interventions if iv.get("status") in ACTIVE_STATUSES}

    # per grade
    grades = {}
    for lid, lr in by_learner.items():
        g = max((int(x["grade"]) for x in lr if x.get("grade") is not None), default=None)
        if g is None:
            continue
        d = grades.setdefault(g, {"learners": 0, "marks": [], "atts": [], "high": 0})
        d["learners"] += 1
        d["marks"] += [num(x.get("sba_mark")) for x in lr]
        d["atts"] += [num(x.get("attendance_rate")) for x in lr]
        d["high"] += 1 if risks[lid]["level"] == "high" else 0
    by_grade = []
    for g in sorted(grades):
        d = grades[g]
        m, a = _avg(d["marks"]), _avg(d["atts"])
        by_grade.append({"grade": g, "learners": d["learners"], "avg_sba": None if m is None else round(m, 1),
                         "attendance_pct": None if a is None else round(a * 100, 1), "high_risk": d["high"]})

    # per subject
    subj = {}
    for r in rows:
        m = num(r.get("sba_mark"))
        if m is None:
            continue
        subj.setdefault(r["subject"], []).append(m)
    by_subject = sorted(
        [{"subject": s, "n": len(v), "avg_sba": round(sum(v) / len(v), 1),
          "pass_rate": round(100 * sum(1 for x in v if x >= PASS_MARK) / len(v), 1)} for s, v in subj.items()],
        key=lambda x: x["avg_sba"])

    # attendance buckets (per learner)
    buckets = {"Below 70%": 0, "70-79%": 0, "80-89%": 0, "90%+": 0}
    for r in risks.values():
        a = r["attendance_pct"]
        if a is None:
            continue
        buckets["Below 70%" if a < 70 else "70-79%" if a < 80 else "80-89%" if a < 90 else "90%+"] += 1

    # trend across terms
    trend = []
    for (y, t) in periods[-8:]:
        pr = [r for r in history if int(r["year"]) == y and int(r["term"]) == t]
        m, a = _avg([num(r.get("sba_mark")) for r in pr]), _avg([num(r.get("attendance_rate")) for r in pr])
        trend.append({"label": period_label(y, t), "year": y, "term": t,
                      "avg_sba": None if m is None else round(m, 1),
                      "attendance_pct": None if a is None else round(a * 100, 1),
                      "learners": len({r["id_number"] for r in pr})})

    at_risk = []
    for lid, r in sorted(risks.items(), key=lambda kv: -kv[1]["score"]):
        if r["level"] == "low":
            break
        at_risk.append({"id_number": lid, "name": names.get(lid, ""), "has_active_intervention": lid in active_ids,
                        "suggested": suggest_interventions(r), **r})
    top = at_risk[:100]

    status_counts = {s: 0 for s in ("planned", "in_progress", "completed", "cancelled")}
    for iv in interventions:
        if iv.get("status") in status_counts:
            status_counts[iv["status"]] += 1

    all_m = [num(r.get("sba_mark")) for r in rows]
    all_a = [num(r.get("attendance_rate")) for r in rows]
    m, a = _avg(all_m), _avg(all_a)
    return {
        "empty": not rows,
        "period": {"year": year, "term": term, "label": period_label(year, term)},
        "available_periods": [{"year": y, "term": t, "label": period_label(y, t)} for (y, t) in periods],
        "learners": len(by_learner),
        "records": len(rows),
        "avg_sba": None if m is None else round(m, 1),
        "attendance_pct": None if a is None else round(a * 100, 1),
        "risk": dist,
        "by_grade": by_grade,
        "by_subject": by_subject,
        "attendance_buckets": [{"label": k, "learners": v} for k, v in buckets.items()],
        "trend": trend,
        "at_risk": top,
        "at_risk_total": len(at_risk),
        "interventions": {
            "counts": status_counts,
            "high_risk_without_active": sum(1 for r in at_risk if r["level"] == "high" and not r["has_active_intervention"]),
            "impact": intervention_impact(history, interventions),
        },
        "notes": ["Behaviour/discipline data is not part of the SA-SAMS export, so risk is based on marks, attendance and their trend."],
    }

# ---------------------------------------------------------------------------
# Recommendations: rules always; the small language model adds a written summary.
# ---------------------------------------------------------------------------
def rule_recommendations(s):
    recs = []
    if s["empty"]:
        return recs
    def add(priority, category, title, detail):
        recs.append({"priority": priority, "category": category, "title": title, "detail": detail})

    n = max(1, s["learners"])
    high, med = s["risk"]["high"], s["risk"]["medium"]
    unassigned = s["interventions"]["high_risk_without_active"]
    if unassigned:
        add("high", "Interventions", f"Open interventions for {unassigned} high-risk learner{'s' if unassigned != 1 else ''}",
            "These learners have no planned or in-progress support. Start with the Interventions tab and use the suggested actions.")
    if high / n >= 0.15:
        add("high", "School-wide", f"{high} of {s['learners']} learners ({100 * high // n}%) are high risk",
            "This is a school-wide pattern, not only individual cases. Raise it with the SMT and the School-Based Support Team.")
    weak = [x for x in s["by_subject"] if x["n"] >= 5 and (x["pass_rate"] < 60 or x["avg_sba"] < 50)][:3]
    for w in weak:
        add("high" if w["pass_rate"] < 40 else "medium", "Academic",
            f"Strengthen {w['subject']}",
            f"Average SBA is {w['avg_sba']}% and {w['pass_rate']}% of learners are at or above 50%. Consider extra lessons, teacher support or moderated re-assessment.")
    low_att = next((b["learners"] for b in s["attendance_buckets"] if b["label"] == "Below 70%"), 0)
    mid_att = next((b["learners"] for b in s["attendance_buckets"] if b["label"] == "70-79%"), 0)
    if low_att + mid_att:
        add("high" if low_att else "medium", "Attendance",
            f"{low_att + mid_att} learners attend less than 80% of the time",
            "Contact parents/guardians, review the reasons for absence and agree attendance plans. Persistent absence often comes before academic decline.")
    worst = sorted([g for g in s["by_grade"] if g["avg_sba"] is not None], key=lambda g: g["avg_sba"])[:1]
    if worst and worst[0]["avg_sba"] < 55:
        add("medium", "Academic", f"Grade {worst[0]['grade']} has the lowest average ({worst[0]['avg_sba']}%)",
            "Review assessment tasks and pacing for this grade with the relevant heads of department.")
    tr = [t for t in s["trend"] if t["avg_sba"] is not None]
    if len(tr) >= 2 and tr[-2]["avg_sba"] - tr[-1]["avg_sba"] >= 3:
        add("medium", "Trend", f"Average SBA fell {tr[-2]['avg_sba'] - tr[-1]['avg_sba']:.1f} points since {tr[-2]['label']}",
            "Check whether this reflects harder assessments, curriculum pace or a real decline before acting.")
    imp = s["interventions"]["impact"]
    if imp["learners_measured"] >= 3 and imp["avg_sba_change"] is not None:
        direction = "improved" if imp["avg_sba_change"] > 0 else "did not improve"
        add("low", "Interventions", f"Supported learners' marks {direction} ({imp['avg_sba_change']:+.1f} points)",
            f"{imp['improved']} of {imp['learners_measured']} supported learners are above their starting mark. This shows movement, not proof the support caused it.")
    if med and not recs:
        add("low", "Monitoring", f"Keep an eye on {med} medium-risk learners", "Re-check them after the next term's upload.")
    if not recs:
        add("low", "Monitoring", "No urgent concerns found", "Keep uploading each term so trends can be tracked.")
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(recs, key=lambda r: order[r["priority"]])

def anonymised_metrics(s):
    """What the language model is allowed to see: school-level numbers only.
    No names, no ID numbers, no free text (learners are minors, POPIA)."""
    return {
        "period": s["period"]["label"], "learners": s["learners"], "avg_sba": s["avg_sba"],
        "attendance_pct": s["attendance_pct"], "risk_counts": s["risk"],
        "weakest_subjects": [{k: x[k] for k in ("subject", "avg_sba", "pass_rate")} for x in s["by_subject"][:5]],
        "by_grade": [{k: g[k] for k in ("grade", "learners", "avg_sba", "attendance_pct", "high_risk")} for g in s["by_grade"]],
        "attendance_buckets": s["attendance_buckets"],
        "trend": [{k: t[k] for k in ("label", "avg_sba", "attendance_pct")} for t in s["trend"][-4:]],
        "high_risk_without_intervention": s["interventions"]["high_risk_without_active"],
        "intervention_counts": s["interventions"]["counts"],
    }

LLM_SYSTEM_PROMPT = (
    "You are an education analyst helping the management team of a South African school. "
    "You receive anonymised school-level statistics as JSON. Write a short plain-English briefing: "
    "first 2 sentences on the overall picture, then exactly 4 bullet points of practical recommendations. "
    "Each bullet starts with a verb and names the specific subject or grade it is about. "
    "Use only numbers present in the data; never invent figures. Do not mention individual learners. "
    "SBA means school-based assessment; the pass mark is 50%. Maximum 170 words."
)

def llm_endpoint() -> str:
    if LLM_URL.endswith("/chat/completions"):
        return LLM_URL
    if LLM_URL.endswith("/v1"):
        return LLM_URL + "/chat/completions"
    return LLM_URL + "/v1/chat/completions"

def call_llm(metrics: dict):
    """Returns the model's briefing text, or raises. Only anonymised metrics are sent."""
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(metrics, separators=(",", ":"))},
        ],
        "temperature": 0.3, "max_tokens": 450, "stream": False,
    }
    headers = {"Content-Type": "application/json", "User-Agent": "DataWrangler/1.0"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    req = urllib.request.Request(llm_endpoint(), data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as response:
        data = json.loads(response.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"] or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"<[^>]*>", "", text)          # never pass markup on to the page
    if len(text) < 20:
        raise ValueError("Model returned an empty answer.")
    return text[:2000]

# ---------------------------------------------------------------------------
# Interventions
# ---------------------------------------------------------------------------
INTERVENTION_CATEGORIES = ("Academic", "Attendance", "Wellbeing", "Referral", "Other")
INTERVENTION_STATUSES = ("planned", "in_progress", "completed", "cancelled")

def clean_date(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError("Due date must look like 2026-08-31.")
    return value

def clean_intervention_fields(data: dict, creating: bool):
    out = {}
    if creating or "title" in data:
        title = sanitize_string(data.get("title", ""))[:120]
        if not title:
            raise ValueError("A title is required.")
        out["title"] = title
    if creating or "category" in data:
        cat = str(data.get("category") or "Academic")
        if cat not in INTERVENTION_CATEGORIES:
            raise ValueError("Unknown category.")
        out["category"] = cat
    if creating or "status" in data:
        st = str(data.get("status") or "planned")
        if st not in INTERVENTION_STATUSES:
            raise ValueError("Unknown status.")
        out["status"] = st
    if "notes" in data:
        out["notes"] = sanitize_string(data.get("notes", ""))[:1000]
    if "outcome" in data:
        out["outcome"] = sanitize_string(data.get("outcome", ""))[:1000]
    if "due_date" in data:
        out["due_date"] = clean_date(data.get("due_date"))
    return out

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Gemini (hosted AI) client + validation diagnosis
# Only anonymised counts and school-level statistics ever go into a prompt.
# ---------------------------------------------------------------------------
DIAGNOSTIC_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "severity": {"type": "STRING", "enum": ["low", "medium", "high"]},
        "patterns": {"type": "ARRAY", "items": {"type": "STRING"}},
        "recommendations": {"type": "ARRAY", "items": {"type": "STRING"}},
        "manual_review_required": {"type": "BOOLEAN"},
    },
    "required": ["summary", "severity", "patterns", "recommendations", "manual_review_required"],
}

DIAGNOSTIC_SYSTEM_PROMPT = (
    "You are The Data Wrangler's data-quality diagnostic assistant. Analyze only the "
    "aggregated anonymised validation summary provided. Do not infer individual learner "
    "characteristics, invent data, or claim to inspect rows. Recommend data-quality "
    "corrections and workflow interventions, not learner-level decisions. Identity "
    "data must be manually verified against official records. Return the specified JSON."
)

def _lower_types(node):
    """The newer Gemini schema format uses lower-case type names."""
    if isinstance(node, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _lower_types(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [_lower_types(x) for x in node]
    return node

# Google has changed the generateContent request format between model generations, and
# Gemini 4 models cannot switch thinking off (thinking tokens count against the output cap).
# So each request is tried in up to four styles, newest first, and the style that works is remembered.
_GEMINI_STYLES = {
    "json": [("new", True, False), ("legacy", True, False), ("plain", True, False), ("plain", False, True)],
    "text": [("plain", True, False), ("plain", False, True)],
}
_gemini_style = {"json": 0, "text": 0}

def _gemini_body(system, user_text, schema, fmt, think, fold):
    cfg = {"maxOutputTokens": GEMINI_MAX_TOKENS}
    if think and GEMINI_THINKING:
        cfg["thinkingConfig"] = {"thinkingLevel": GEMINI_THINKING}
    prompt = user_text
    if schema is not None:
        if fmt == "new":
            cfg["responseFormat"] = {"text": {"mimeType": "APPLICATION_JSON", "schema": _lower_types(schema)}}
        elif fmt == "legacy":
            cfg["responseMimeType"] = "application/json"
            cfg["responseSchema"] = schema
        else:
            prompt += "\n\nReturn ONLY one JSON object with the keys: " + ", ".join(schema["required"]) + ". No markdown."
    text = (system + "\n\n" + prompt) if fold else prompt
    body = {"contents": [{"role": "user", "parts": [{"text": text}]}], "generationConfig": cfg}
    if not fold:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    return body

_GEMINI_RETRY_CODES = (429, 500, 502, 503, 504)   # temporary on Google's side: worth another try

def _gemini_call(model, system, user_text, schema):
    """One model, all request styles, with short retries for temporary overload."""
    mode = "json" if schema is not None else "text"
    styles = _GEMINI_STYLES[mode]
    url = f"{GEMINI_API_BASE}/models/{urllib.parse.quote(model, safe='')}:generateContent"
    last = "no attempt"
    i = _gemini_style[mode]
    retries = 0
    while i < len(styles):
        body = json.dumps(_gemini_body(system, user_text, schema, *styles[i])).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY})
        try:
            with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Response bodies are not logged: they can contain provider details.
            LOGGER.warning("Gemini HTTP %s (model %s, request style %s)", exc.code, model, styles[i])
            last = f"HTTP {exc.code}"
            if exc.code == 400 and i + 1 < len(styles):
                i += 1                          # this model rejected that request style: try the next
                continue
            if exc.code in _GEMINI_RETRY_CODES and retries < GEMINI_RETRIES:
                retries += 1
                time.sleep(1.5 * retries)       # 1.5s, then 3s
                continue
            raise RuntimeError(f"AI provider error ({last}).") from exc
        except Exception as exc:
            raise RuntimeError(f"AI provider unreachable ({type(exc).__name__}).") from exc
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        if not text:
            reason = cand.get("finishReason") or (data.get("promptFeedback") or {}).get("blockReason") or "empty"
            raise RuntimeError(f"AI returned no text ({reason}).")
        _gemini_style[mode] = i
        return text
    raise RuntimeError(f"AI provider rejected every request style ({last}).")

def gemini_generate(system, user_text, schema=None):
    """Calls Gemini and returns the answer text. Raises RuntimeError with a safe message.
    Temporary overload (503/429) is retried; then the optional fallback model is tried."""
    if not GEMINI_API_KEY:
        raise RuntimeError("Gemini is not configured.")
    models = [GEMINI_MODEL] + ([GEMINI_FALLBACK_MODEL] if GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL != GEMINI_MODEL else [])
    error = None
    for model in models:
        try:
            return _gemini_call(model, system, user_text, schema)
        except RuntimeError as exc:
            error = exc
    raise error

def parse_json_object(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise ValueError("No JSON object in the model answer.")
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("Model response was not a JSON object.")
    return obj

def categorize_error(error_text):
    # Only category counts leave the backend; row-specific text and identifiers never do.
    text = error_text.lower()
    if "checksum" in text or "13 digits" in text or "birth date" in text:
        return "identity_validation"
    if "subject" in text:
        return "subject_validation"
    if "grade" in text:
        return "grade_validation"
    if "sba" in text:
        return "sba_validation"
    if "attendance" in text:
        return "attendance_validation"
    if "columns" in text:
        return "column_structure"
    if "numeric" in text:
        return "numeric_parsing"
    return "other"

def build_diagnostic_payload(valid_records, errors):
    counts = Counter(categorize_error(item.get("error", "")) for item in errors)
    return {
        "total_rows_processed": len(valid_records) + len(errors),
        "valid_rows": len(valid_records),
        "invalid_rows": len(errors),
        "error_categories": dict(counts),
    }

def hydrogen_diagnose(payload):
    text = gemini_generate(DIAGNOSTIC_SYSTEM_PROMPT, json.dumps(payload, separators=(",", ":")), DIAGNOSTIC_SCHEMA)
    try:
        diagnosis = parse_json_object(text)
        # The answer is untrusted: check its shape, cap its size, never pass markup on.
        if any(key not in diagnosis for key in DIAGNOSTIC_SCHEMA["required"]):
            raise ValueError("Model response omitted required fields.")
    except ValueError as exc:
        LOGGER.warning("Gemini response failed validation: %s", exc)
        raise RuntimeError("AI diagnostic response was invalid.") from exc
    if diagnosis["severity"] not in ("low", "medium", "high"):
        diagnosis["severity"] = "medium"
    for key in ("patterns", "recommendations"):
        if not isinstance(diagnosis[key], list):
            diagnosis[key] = []
        diagnosis[key] = [re.sub(r"<[^>]*>", "", str(x))[:500] for x in diagnosis[key][:10]]
    diagnosis["summary"] = re.sub(r"<[^>]*>", "", str(diagnosis["summary"]))[:1200]
    diagnosis["manual_review_required"] = bool(diagnosis["manual_review_required"])
    return diagnosis

CATEGORY_ADVICE = {
    "identity_validation": ("ID numbers that fail the length, birth-date or checksum test",
                            "Check these ID numbers against the learners' official records in SA-SAMS. Do not guess or edit digits."),
    "subject_validation": ("Subject names SA-SAMS does not recognise",
                           "Use the exact subject names from the SA-SAMS export, including the language level (Home Language or First Additional)."),
    "grade_validation": ("Grades outside 1 to 12", "Check the grade column for typos or a shifted column."),
    "sba_validation": ("SBA marks outside 0 to 100", "Marks must be between 0 and 100. Look for percentages entered twice or totals instead of percentages."),
    "attendance_validation": ("Attendance outside 0.0 to 1.0", "Attendance must be a fraction such as 0.92, not a percentage such as 92."),
    "column_structure": ("Rows that do not have exactly 7 columns", "Look for extra commas, a header row, merged cells or a column that was removed."),
    "numeric_parsing": ("Grades, marks or attendance that are not numbers", "Look for text, empty attendance cells or commas used as decimal points."),
    "other": ("Other validation problems", "Open the issues list for the details."),
}

def rule_diagnosis(payload):
    """The same answer shape as the AI diagnosis, produced by plain rules. Used when the AI is unavailable."""
    total, bad = payload["total_rows_processed"], payload["invalid_rows"]
    ratio = bad / total if total else 0
    cats = sorted(payload["error_categories"].items(), key=lambda kv: -kv[1])
    if not bad:
        return {"summary": f"No validation errors were found in {total} rows. The file looks ready to submit.",
                "severity": "low", "patterns": [],
                "recommendations": ["Commit the upload.", "Spot-check a few learners against SA-SAMS before submitting to the DDD dashboard."],
                "manual_review_required": False}
    return {
        "summary": f"{bad} of {total} rows ({round(100 * ratio)}%) failed validation. The most common problem is: {CATEGORY_ADVICE.get(cats[0][0], CATEGORY_ADVICE['other'])[0].lower()}.",
        "severity": "high" if ratio >= 0.3 else "medium" if ratio >= 0.1 else "low",
        "patterns": [f"{CATEGORY_ADVICE.get(c, CATEGORY_ADVICE['other'])[0]}: {n} row{'s' if n != 1 else ''}" for c, n in cats[:6]],
        "recommendations": [CATEGORY_ADVICE.get(c, CATEGORY_ADVICE["other"])[1] for c, _ in cats[:5]],
        "manual_review_required": "identity_validation" in payload["error_categories"],
    }

def call_gemini_briefing(metrics: dict):
    text = gemini_generate(LLM_SYSTEM_PROMPT, json.dumps(metrics, separators=(",", ":")))
    text = re.sub(r"<[^>]*>", "", text).strip()
    if len(text) < 20:
        raise RuntimeError("AI returned an empty answer.")
    return text[:2000]

class DataWranglerServer(BaseHTTPRequestHandler):
    server_version = "DataWrangler/1.0"

    def log_message(self, format_string, *args):
        # Default access logs can contain URLs/tokens. Log only method and route.
        LOGGER.info("%s %s", self.command, self.path.split("?", 1)[0])

    def _origin(self):
        if ALLOWED_ORIGINS == "*":
            return "*"
        allowed = [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]
        origin = self.headers.get("Origin", "")
        return origin if origin in allowed else (allowed[0] if allowed else "")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self._origin())
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-Wrangler-Token, X-School-ID, Content-Type")
        self.end_headers()

    # -- small response / request helpers ---------------------------------
    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", self._origin())
        self.send_header("Vary", "Origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, max_bytes: int = 10_000) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length < 0 or length > max_bytes:
            raise ValueError("Request too large.")
        data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("Invalid request.")
        return data

    # -- REGISTRATION -------------------------------------------------------
    def _register(self):
        ip = (self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self.client_address[0])
        if not (rate_ok(f"reg:{ip}", REGISTER_PER_IP_PER_HOUR) and rate_ok("reg:global", REGISTER_GLOBAL_PER_HOUR)):
            return self._json(429, {"detail": "Too many registrations. Please try again later."})
        try:
            data = self._read_json()
            school_name = sanitize_string(data.get("school_name", ""))
            if not school_name:
                raise ValueError("School name is required.")
            email = clean_email(data.get("email"))

            new_token = f"school-{secrets.token_hex(8)}"
            record = {"school_name": school_name, "api_token": new_token}
            if email:
                record["contact_email"] = email  # needs the contact_email column (see setup SQL)

            req = urllib.request.Request(
                f"{SUPABASE_URL}/rest/v1/sasams_schools",
                data=json.dumps(record).encode("utf-8"),
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=representation"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as response:
                response.read()
        except urllib.error.HTTPError as e:
            # THIS WILL SHOW IN RENDER LOGS TO DEBUG DATABASE REJECTIONS
            log(f"DEBUG: Registration failed: HTTP {e.code} {e.read().decode('utf-8', 'ignore')[:300]}")
            return self._json(400, {"detail": "Registration failed: the database rejected the request (see server logs)."})
        except json.JSONDecodeError:
            return self._json(400, {"detail": "Registration failed: invalid request."})
        except Exception as e:
            log(f"DEBUG: Registration failed: {str(e)}")
            return self._json(400, {"detail": f"Registration failed: {str(e)}"})

        emailed = False
        if email and EMAIL_ENABLED:
            if rate_ok(f"email:{email}", EMAIL_PER_ADDRESS_PER_HOUR) and rate_ok("email:global", EMAIL_GLOBAL_PER_HOUR):
                emailed = send_email(email, "Your Data Wrangler school token", build_token_email([(school_name, new_token)]))
                log(f"DEBUG: Registration email to {mask_email(email)}: {'sent' if emailed else 'FAILED'}")
        self._json(201, {"token": new_token, "school_name": school_name, "emailed": emailed})

    # -- LOGIN: checks the token and tells the app which school it belongs to
    def _login(self):
        token = self.headers.get("X-Wrangler-Token", "").strip()
        if not token:
            return self._json(403, {"detail": "No token provided"})
        try:
            school = find_school_by_token(token)
        except Exception as e:
            log(f"DEBUG: Login DB Error: {str(e)}")
            return self._json(500, {"detail": "Could not reach the database."})
        if not school:
            return self._json(403, {"detail": "Invalid School Token"})
        self._json(200, {"school_name": school["school_name"], "school_id": school["id"]})

    # -- SEND TOKEN TO EMAIL ------------------------------------------------
    def _send_token(self):
        if not EMAIL_ENABLED:
            return self._json(503, {"detail": "Email sending is not set up on this server yet."})
        try:
            email = clean_email(self._read_json().get("email"))
            if not email:
                raise ValueError("Please enter your registered email address.")
        except json.JSONDecodeError:
            return self._json(400, {"detail": "Invalid request."})
        except ValueError as e:
            return self._json(400, {"detail": str(e)})

        # Limits apply to every address (registered or not), so they reveal nothing.
        if not rate_ok(f"email:{email}", EMAIL_PER_ADDRESS_PER_HOUR) or not rate_ok("email:global", EMAIL_GLOBAL_PER_HOUR):
            return self._json(429, {"detail": "Too many requests. Please try again later."})

        try:
            safe = urllib.parse.quote(email, safe="")
            rows = supabase_get(f"sasams_schools?contact_email=eq.{safe}&select=school_name,api_token&order=created_at.desc&limit=5")
        except Exception as e:
            log(f"DEBUG: Send-token DB Error: {str(e)}")
            return self._json(500, {"detail": "Could not reach the database."})

        if rows:
            ok = send_email(email, "Your Data Wrangler school token", build_token_email([(r["school_name"], r["api_token"]) for r in rows]))
            log(f"DEBUG: Token email to {mask_email(email)}: {'sent' if ok else 'FAILED'}")
        else:
            log(f"DEBUG: Token email requested for unregistered address {mask_email(email)}")
        # Same answer whether or not the address is registered (no account enumeration).
        self._json(200, {"detail": "If that email is registered, we have sent the token to it."})

    # -- PHASE 3 ------------------------------------------------------------
    def _auth(self):
        """Returns the school for this request's token, or sends the error and returns None.
        Every Phase 3 query is filtered by the returned school id (tenant isolation)."""
        token = (self.headers.get("X-Wrangler-Token") or "").strip()
        if not token:
            self._json(403, {"detail": "No token provided"})
            return None
        try:
            school = find_school_by_token(token)
        except Exception as e:
            log(f"DEBUG: Auth DB Error: {str(e)}")
            self._json(500, {"detail": "Could not reach the database."})
            return None
        if not school:
            self._json(403, {"detail": "Invalid School Token"})
            return None
        return school

    def _guard(self, fn, *args):
        try:
            return fn(*args)
        except ValueError as e:
            return self._json(400, {"detail": str(e)})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            log(f"DEBUG: Database error {e.code}: {body}")
            if e.code == 404 or "PGRST205" in body or "does not exist" in body:
                return self._json(503, {"detail": "The Phase 3 tables are missing. Run the Phase 3 SQL in Supabase first."})
            return self._json(500, {"detail": "Database error (see server logs)."})
        except Exception as e:
            log(f"DEBUG: Unhandled error: {type(e).__name__}: {str(e)[:200]}")
            return self._json(500, {"detail": "Server error."})

    def _summary_for(self, school_id, qs):
        history = load_history(school_id)
        periods = periods_in(history)
        fallback = periods[-1] if periods else default_period()      # default: newest uploaded term
        year, term = parse_period(qs, fallback)
        return build_summary(history, year, term, load_names(school_id), load_interventions(school_id)), history

    def _analytics_summary(self, school, qs):
        summary, _ = self._summary_for(school["id"], qs)
        summary["school_name"] = school["school_name"]
        self._json(200, summary)

    def _learner_profile(self, school, id_number):
        if not re.match(r"^\d{13}$", id_number):
            raise ValueError("Invalid ID number.")
        sid = school["id"]
        history = [r for r in load_history(sid) if r["id_number"] == id_number]
        if not history:
            return self._json(404, {"detail": "Learner not found in this school's history."})
        periods = periods_in(history)
        profile = []
        for (y, t) in periods:
            rows = [r for r in history if int(r["year"]) == y and int(r["term"]) == t]
            risk = learner_risk(rows, [r for r in history if (int(r["year"]), int(r["term"])) == prev_period(y, t)])
            profile.append({"label": period_label(y, t), "risk": risk["level"], "avg_sba": risk["avg_sba"],
                            "attendance_pct": risk["attendance_pct"],
                            "subjects": [{"subject": r["subject"], "sba_mark": num(r.get("sba_mark"))} for r in rows]})
        latest_rows = [r for r in history if (int(r["year"]), int(r["term"])) == periods[-1]]
        prev_rows = [r for r in history if (int(r["year"]), int(r["term"])) == prev_period(*periods[-1])]
        risk = learner_risk(latest_rows, prev_rows)
        ivs = [iv for iv in load_interventions(sid) if iv["id_number"] == id_number]
        self._json(200, {"id_number": id_number, "name": load_names(sid).get(id_number, ""),
                         "periods": profile, "risk": risk, "suggested": suggest_interventions(risk),
                         "interventions": ivs})

    def _interventions_list(self, school, qs):
        rows = load_interventions(school["id"])
        status, lid = qget(qs, "status"), qget(qs, "id_number")
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if lid:
            rows = [r for r in rows if r.get("id_number") == lid]
        names = load_names(school["id"])
        self._json(200, {"interventions": [{**r, "name": names.get(r["id_number"], "")} for r in rows[:500]],
                         "total": len(rows)})

    def _intervention_create(self):
        school = self._auth()
        if not school:
            return
        sid = school["id"]
        data = self._read_json(5000)
        id_number = str(data.get("id_number", "")).strip()
        if not re.match(r"^\d{13}$", id_number):
            raise ValueError("A valid 13-digit ID number is required.")
        # The learner must belong to THIS school, or the request is refused.
        known = supabase_request("GET", f"sasams_students?school_id=eq.{q(sid)}&id_number=eq.{q(id_number)}&select=id_number&limit=1")
        if not known:
            return self._json(404, {"detail": "That learner is not in your school's data."})
        row = clean_intervention_fields(data, creating=True)
        history = load_history(sid)
        periods = periods_in(history)
        year, term = parse_period({k: [str(v)] for k, v in data.items() if k in ("year", "term") and v}, periods[-1] if periods else default_period())
        row.update({"school_id": sid, "id_number": id_number, "year": year, "term": term,
                    "due_date": clean_date(data.get("due_date")), "notes": sanitize_string(data.get("notes", ""))[:1000]})
        if row["status"] == "completed":
            row["completed_at"] = now_iso()
        created = supabase_request("POST", "sasams_interventions", row, prefer="return=representation")
        cache_drop(sid)
        log(f"AUDIT: school={sid} created intervention for learner ***{id_number[-4:]}")
        self._json(201, {"intervention": (created or [row])[0]})

    def _intervention_update(self, iv_id):
        school = self._auth()
        if not school:
            return
        sid = school["id"]
        fields = clean_intervention_fields(self._read_json(5000), creating=False)
        if not fields:
            raise ValueError("Nothing to update.")
        fields["updated_at"] = now_iso()
        if fields.get("status") == "completed":
            fields["completed_at"] = now_iso()
        # Filtering on school_id as well as id means one school can never edit another's rows.
        updated = supabase_request("PATCH", f"sasams_interventions?id=eq.{int(iv_id)}&school_id=eq.{q(sid)}",
                                   fields, prefer="return=representation")
        cache_drop(sid)
        if not updated:
            return self._json(404, {"detail": "Intervention not found."})
        log(f"AUDIT: school={sid} updated intervention {int(iv_id)}")
        self._json(200, {"intervention": updated[0]})

    def _intervention_delete(self, iv_id):
        school = self._auth()
        if not school:
            return
        sid = school["id"]
        deleted = supabase_request("DELETE", f"sasams_interventions?id=eq.{int(iv_id)}&school_id=eq.{q(sid)}", prefer="return=representation")
        cache_drop(sid)
        if not deleted:
            return self._json(404, {"detail": "Intervention not found."})
        log(f"AUDIT: school={sid} deleted intervention {int(iv_id)}")
        self._json(200, {"deleted": True})

    def _recommendations(self, qs):
        school = self._auth()
        if not school:
            return
        sid = school["id"]
        summary, _ = self._summary_for(sid, qs)
        recs = rule_recommendations(summary)
        result = {"period": summary["period"], "recommendations": recs, "narrative": None, "model": None, "model_note": ""}
        if summary["empty"]:
            return self._json(200, result)
        provider = "llm" if LLM_URL else "gemini" if GEMINI_API_KEY else None
        if not provider:
            result["model_note"] = "No AI model is connected. These are rule-based recommendations."
            return self._json(200, result)

        metrics = anonymised_metrics(summary)
        ckey = (str(sid), "narrative", json.dumps(metrics, sort_keys=True))
        cached = cache_get(ckey)
        if cached is None:
            if not rate_ok(f"llm:{sid}", LLM_PER_SCHOOL_PER_HOUR) or (provider == "gemini" and not rate_ok("gemini:global", GEMINI_GLOBAL_PER_HOUR)):
                result["model_note"] = "AI summary limit reached for this hour. Showing rule-based recommendations."
                return self._json(200, result)
            try:
                cached = call_llm(metrics) if provider == "llm" else call_gemini_briefing(metrics)
                with _cache_lock:               # narratives stay valid for as long as the data does
                    _cache[ckey] = (time.time() + 3600 - CACHE_TTL, cached)
            except Exception as e:
                log(f"DEBUG: LLM call failed: {type(e).__name__}: {str(e)[:200]}")
                result["model_note"] = "The AI model could not be reached. Showing rule-based recommendations."
                return self._json(200, result)
        result.update({"narrative": cached, "model": LLM_MODEL if provider == "llm" else GEMINI_MODEL})
        self._json(200, result)

    def _diag_reply(self, school_id, qs, payload, out):
        """Sends the diagnosis, and remembers it for that term's School Report when year/term were given."""
        if qget(qs, "year") or qget(qs, "term"):
            try:
                y, t = parse_period(qs)
                save_term_report(school_id, y, t, validation=payload, diagnosis=out["diagnosis"],
                                 diagnosis_source=out["source"], diagnosis_model=out["model"])
            except ValueError:
                raise
            except Exception as e:
                log(f"DEBUG: Diagnosis save failed (run the Phase 3 SQL?): {str(e)[:200]}")
        self._json(200, out)

    def _diagnose(self, school_id, valid_records, errors, qs):
        payload = build_diagnostic_payload(valid_records, errors)
        if qget(qs, "year") or qget(qs, "term"):
            parse_period(qs)               # reject a bad term/year before spending an AI call
        if payload["total_rows_processed"] == 0:
            raise ValueError("The file has no rows to diagnose.")
        out = {"status": "diagnosed", "mode": "AI Diagnostic", "validation": payload,
               "source": "rules", "model": None, "note": "", "diagnosis": None}
        if not GEMINI_API_KEY:
            out.update({"diagnosis": rule_diagnosis(payload), "note": "No AI model is connected. This is a rule-based diagnosis."})
            return self._diag_reply(school_id, qs, payload, out)
        ckey = (str(school_id), "diagnosis", json.dumps(payload, sort_keys=True))
        diagnosis = cache_get(ckey)
        if diagnosis is None:
            if not (rate_ok(f"diag:{school_id}", GEMINI_PER_SCHOOL_PER_HOUR) and rate_ok("gemini:global", GEMINI_GLOBAL_PER_HOUR)):
                out.update({"diagnosis": rule_diagnosis(payload), "note": "AI diagnosis limit reached for this hour. Showing a rule-based diagnosis."})
                return self._diag_reply(school_id, qs, payload, out)
            try:
                diagnosis = hydrogen_diagnose(payload)
                with _cache_lock:
                    _cache[ckey] = (time.time() + 3600 - CACHE_TTL, diagnosis)
            except Exception as exc:
                LOGGER.warning("AI diagnostic unavailable: %s", str(exc)[:160])
                out.update({"diagnosis": rule_diagnosis(payload), "note": "The AI service could not be reached. Showing a rule-based diagnosis."})
                return self._diag_reply(school_id, qs, payload, out)
        out.update({"diagnosis": diagnosis, "source": "gemini", "model": GEMINI_MODEL})
        self._diag_reply(school_id, qs, payload, out)

    def _report(self, school, qs):
        sid = school["id"]
        summary, _ = self._summary_for(sid, qs)
        out = {"school_name": school["school_name"], "generated_at": now_iso(), "summary": summary}
        if summary["empty"]:
            out["empty"] = True
            return self._json(200, out)
        names = load_names(sid)
        today = (datetime.now(timezone.utc) + timedelta(hours=2)).date().isoformat()     # South African time
        ivs = load_interventions(sid)
        out["interventions"] = [{**iv, "name": names.get(iv["id_number"], "")} for iv in ivs[:300]]
        out["overdue_count"] = sum(1 for iv in ivs if iv.get("status") in ACTIVE_STATUSES and iv.get("due_date") and str(iv["due_date"]) < today)
        out["recommendations"] = rule_recommendations(summary)
        # Reuse a briefing that was already generated; the report itself never spends an AI call.
        narrative = cache_get((str(sid), "narrative", json.dumps(anonymised_metrics(summary), sort_keys=True)))
        out["narrative"] = narrative
        out["narrative_model"] = (LLM_MODEL if LLM_URL else GEMINI_MODEL) if narrative else None
        out["saved"] = None
        try:
            out["saved"] = load_term_report(sid, summary["period"]["year"], summary["period"]["term"])
        except urllib.error.HTTPError as e:
            log(f"DEBUG: Term report read failed: HTTP {e.code} (run the Phase 3 SQL?)")
        self._json(200, out)

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        route, qs = parts.path.rstrip("/"), urllib.parse.parse_qs(parts.query)
        if route == "/api/v1/health":
            return self._json(200, {"status": "ok", "ai_model": bool(LLM_URL or GEMINI_API_KEY), "diagnose_ai": bool(GEMINI_API_KEY), "email": EMAIL_ENABLED})
        m = re.match(r"^/api/v1/learners/(\d{1,20})$", route)
        if route not in ("/api/v1/analytics/summary", "/api/v1/interventions", "/api/v1/report") and not m:
            return self._json(404, {"detail": "Not found"})
        school = self._auth()
        if not school:
            return
        if route == "/api/v1/analytics/summary":
            return self._guard(self._analytics_summary, school, qs)
        if route == "/api/v1/interventions":
            return self._guard(self._interventions_list, school, qs)
        if route == "/api/v1/report":
            return self._guard(self._report, school, qs)
        return self._guard(self._learner_profile, school, m.group(1))

    def do_PATCH(self):
        m = re.match(r"^/api/v1/interventions/(\d{1,12})$", urllib.parse.urlsplit(self.path).path)
        if not m:
            return self._json(404, {"detail": "Not found"})
        self._guard(self._intervention_update, m.group(1))

    def do_DELETE(self):
        m = re.match(r"^/api/v1/interventions/(\d{1,12})$", urllib.parse.urlsplit(self.path).path)
        if not m:
            return self._json(404, {"detail": "Not found"})
        self._guard(self._intervention_delete, m.group(1))

    def do_POST(self):
        parts = urllib.parse.urlsplit(self.path)
        route, qs = parts.path.rstrip("/"), urllib.parse.parse_qs(parts.query)
        if route == "/api/v1/register":
            return self._register()
        if route == "/api/v1/login":
            return self._login()
        if route == "/api/v1/send-token":
            return self._send_token()
        if route == "/api/v1/interventions":
            return self._guard(self._intervention_create)
        if route == "/api/v1/recommendations":
            return self._guard(self._recommendations, qs)

        # --- INGESTION ENDPOINTS ---
        is_dry_run = False
        if route in ("/api/v1/validate", "/api/v1/diagnose"):
            is_dry_run = True
        elif route == "/api/v1/ingest":
            is_dry_run = False
        else:
            return self._json(404, {"detail": "Not found"})

        token = self.headers.get("X-Wrangler-Token")
        if not token:
            return self._json(403, {"detail": "No token provided"})

        school_id = None
        try:
            school = find_school_by_token(token)
        except Exception as e:
            log(f"DEBUG: Auth DB Error: {str(e)}")
            return self._json(500, {"detail": f"Auth DB Error: {str(e)}"})
        if not school:
            return self._json(403, {"detail": "Invalid School Token"})
        school_id = school["id"]

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._json(400, {"detail": "Invalid Content-Length."})
        if content_length <= 0:
            return self._json(400, {"detail": "The upload is empty."})
        if content_length > MAX_UPLOAD_SIZE:
            return self._json(413, {"detail": "File exceeds 5MB limit."})

        raw_body = self.rfile.read(content_length)
        try:
            file_content = parse_csv_upload(raw_body, self.headers.get("Content-Type", ""))
            rows = [row for row in csv.reader(io.StringIO(file_content)) if row and any(c.strip() for c in row)]
        except (ValueError, UnicodeDecodeError, csv.Error):
            return self._json(400, {"detail": "CSV parsing failed. Check the file encoding (UTF-8) and format."})

        valid_records = []
        errors = []
        for idx, row in enumerate(rows, start=1):
            record, error = validate_row(row, idx)
            if record:
                record["school_id"] = school_id
                valid_records.append(record)
            else:
                errors.append({"error": error})

        try:
            year, term = parse_period(qs)
        except ValueError as e:
            return self._json(400, {"detail": str(e)})

        if route == "/api/v1/diagnose":
            return self._guard(self._diagnose, school_id, valid_records, errors, qs)

        if not is_dry_run and valid_records and SUPABASE_KEY:
            target_url = f"{SUPABASE_URL}/rest/v1/sasams_students"
            req = urllib.request.Request(
                target_url,
                data=json.dumps(valid_records).encode('utf-8'),
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal,resolution=merge-duplicates"
                },
                method="POST"
            )
            try:
                with urllib.request.urlopen(req) as response:
                    response.read()
            except urllib.error.HTTPError as e:
                error_body = e.read().decode('utf-8')
                return self._json(500, {"detail": f"Supabase Error: {error_body}"})
            except Exception as e:
                return self._json(500, {"detail": str(e)})

        # Phase 3: keep a per-term snapshot for trends. A failure here must never
        # undo or hide a successful ingest (e.g. the Phase 3 SQL not run yet).
        history_saved = None
        if not is_dry_run and valid_records:
            try:
                history_saved = save_history(school_id, valid_records, year, term)
            except Exception as e:
                log(f"DEBUG: History save failed (run the Phase 3 SQL?): {str(e)[:200]}")
                history_saved = False

        if not is_dry_run and valid_records:
            try:
                save_term_report(school_id, year, term, validation=build_diagnostic_payload(valid_records, errors))
            except Exception as e:
                log(f"DEBUG: Term report save failed (run the Phase 3 SQL?): {str(e)[:200]}")

        self._json(200, {
            "status": "completed",
            "period": {"year": year, "term": term, "label": period_label(year, term)},
            "history_saved": history_saved,
            "mode": "Dry Run (Validation Only)" if is_dry_run else "Live Ingestion",
            "school_id": school_id,
            "valid_count": len(valid_records),
            "errors": errors
        })

def run():
    port = int(os.environ.get("PORT", 8000))
    server_address = ('0.0.0.0', port)
    httpd = ThreadingHTTPServer(server_address, DataWranglerServer)
    log(f"The Data Wrangler Native Engine running on port {port}...")
    log(f"Email sending: {'ENABLED' if EMAIL_ENABLED else 'disabled (set BREVO_API_KEY and MAIL_FROM to enable)'}")
    httpd.serve_forever()

if __name__ == '__main__':
    run()
