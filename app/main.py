import os
import re
import json
import io
import secrets
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.error
from datetime import datetime

# 1. Target Endpoint Config
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("CRITICAL: Missing environment variables. Server refusing to start.")

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
    clean = re.sub(r"<[^>]*>", "", str(v))
    clean = re.sub(r"[;'\"]", "", clean)
    return clean.strip()

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

def validate_row(line: str, index: int):
    fields = [f.strip() for f in line.split(",")]
    
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

def parse_multipart(body: bytes, boundary: str):
    parts = body.split(f"--{boundary}".encode('utf-8'))
    for part in parts:
        if b"filename=" in part and b"\r\n\r\n" in part:
            header_data, file_data = part.split(b"\r\n\r\n", 1)
            file_data = file_data.rstrip(b"\r\n")
            return file_data.decode('utf-8', errors='ignore')
    return ""

class DataWranglerServer(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        # FIX: Added X-School-ID to allowed headers to match the frontend
        self.send_header("Access-Control-Allow-Headers", "X-Wrangler-Token, X-School-ID, Content-Type")
        self.end_headers()

    def do_POST(self):
        # --- REGISTRATION ENDPOINT ---
        if self.path == "/api/v1/register":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            try:
                data = json.loads(body)
                school_name = sanitize_string(data.get("school_name", ""))
                if not school_name:
                    raise ValueError("School name is required.")
                
                new_token = f"school-{secrets.token_hex(8)}"
                target_url = f"{SUPABASE_URL}/rest/v1/sasams_schools"
                
                req = urllib.request.Request(
                    target_url,
                    data=json.dumps({"school_name": school_name, "api_token": new_token}).encode('utf-8'),
                    headers={
                        "apikey": SUPABASE_KEY, 
                        "Authorization": f"Bearer {SUPABASE_KEY}",
                        "Content-Type": "application/json", 
                        "Prefer": "return=representation"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req) as response:
                    response.read()
                
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"token": new_token, "school_name": school_name}).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(400)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"detail": f"Registration failed: {str(e)}"}).encode('utf-8'))
                return

        # --- INGESTION ENDPOINTS ---
        is_dry_run = False
        if self.path == "/api/v1/validate":
            is_dry_run = True
        elif self.path == "/api/v1/ingest":
            is_dry_run = False
        else:
            self.send_response(404)
            self.end_headers()
            return

        token = self.headers.get("X-Wrangler-Token")
        if not token:
            self.send_response(403)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"detail": "No token provided"}')
            return

        school_id = None
        try:
            auth_url = f"{SUPABASE_URL}/rest/v1/sasams_schools?api_token=eq.{token}&select=id"
            req = urllib.request.Request(auth_url, headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"})
            with urllib.request.urlopen(req) as response:
                res_data = json.loads(response.read().decode('utf-8'))
                if len(res_data) > 0:
                    school_id = res_data[0]["id"]
                else:
                    self.send_response(403)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(b'{"detail": "Invalid School Token"}')
                    return
        except Exception as e:
            self.send_response(500)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"detail": f"Auth DB Error: {str(e)}"}).encode('utf-8'))
            return

        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > MAX_UPLOAD_SIZE:
            self.send_response(413)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"detail": "File exceeds 5MB limit."}')
            return

        raw_body = self.rfile.read(content_length)
        raw_lines = []
        try:
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' in content_type:
                boundary = content_type.split("boundary=")[1].strip()
                file_content = parse_multipart(raw_body, boundary)
                raw_lines = file_content.splitlines()
            else:
                raw_lines = raw_body.decode('utf-8').splitlines()
        except Exception as e:
            self.send_response(400)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"detail": f"Parse error: {str(e)}"}).encode('utf-8'))
            return

        clean_lines = [line.strip() for line in raw_lines if line.strip() and not line.startswith("------")]
        valid_records = []
        errors = []

        for idx, line in enumerate(clean_lines):
            record, error = validate_row(line, idx + 1)
            if record:
                record["school_id"] = school_id
                valid_records.append(record)
            else:
                errors.append({"error": error})

        if not is_dry_run and valid_records and SUPABASE_KEY:
            target_url = f"{SUPABASE_URL}/rest/v1/sasams_students"
            req = urllib.request.Request(
                target_url,
                data=json.dumps(valid_records).encode('utf-8'),
                headers={
                    "apikey": SUPABASE_KEY, 
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "application/json", 
                    "Prefer": "return=representation,resolution=merge-duplicates"
                },
                method="POST"
            )
            try:
                with urllib.request.urlopen(req) as response:
                    response.read()
            except urllib.error.HTTPError as e:
                error_body = e.read().decode('utf-8')
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"detail": f"Supabase Error: {error_body}"}).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"detail": str(e)}).encode('utf-8'))
                return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({
            "status": "completed",
            "mode": "Dry Run (Validation Only)" if is_dry_run else "Live Ingestion",
            "school_id": school_id,
            "valid_count": len(valid_records),
            "errors": errors
        }).encode('utf-8'))

def run():
    port = int(os.environ.get("PORT", 8000))
    server_address = ('0.0.0.0', port)
    httpd = HTTPServer(server_address, DataWranglerServer)
    print(f"The Data Wrangler Native Engine running on port {port}...")
    httpd.serve_forever()

if __name__ == '__main__':
    run()