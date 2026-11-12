import os
import re
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request

# 1. Target Endpoint Config (Points straight to Supabase REST API)
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://reqzqlzphirvqoaycbkl.supabase.co")
# Reads your Supabase anon/service API key safely from your environment
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

MASTER_TOKEN = os.getenv("WRANGLER_TOKEN", "WranglerSecureToken2026_NC")

def sanitize_string(v: str) -> str:
    clean = re.sub(r"<[^>]*>", "", str(v))  # Mitigate XSS attacks
    clean = re.sub(r"[;'\"]", "", clean)    # Mitigate basic SQL Injections
    return clean.strip()

def validate_row(line: str, index: int):
    fields = [f.strip() for f in line.split(",")]
    if len(fields) != 6:
        return None, f"Line {index}: Format Mismatch. Expected 6 parameters, got {len(fields)}"
    
    id_num, first, last, grade, sba, att = fields
    
    if not re.match(r"^\d{13}$", id_num):
        return None, f"Line {index}: ID must be exactly 13 digits."
        
    try:
        grade_int = int(grade)
        if not (1 <= grade_int <= 12):
            return None, f"Line {index}: Grade must be between 1 and 12."
            
        sba_float = float(sba) if sba else None
        if sba_float is not None and not (0.0 <= sba_float <= 100.0):
            return None, f"Line {index}: Highlighted SBA mark must be 0-100."
            
        att_float = float(att)
        if not (0.0 <= att_float <= 1.0):
            return None, f"Line {index}: Attendance rate must be between 0.0 and 1.0."
    except ValueError:
        return None, f"Line {index}: Numeric parsing failure."

    return {
        "id_number": id_num,
        "first_name": sanitize_string(first),
        "last_name": sanitize_string(last),
        "grade": grade_int,
        "sba_mark": sba_float,
        "attendance_rate": att_float
    }, None

class DataWranglerServer(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api/v1/ingest":
            self.send_response(404)
            self.end_headers()
            return

        # Check API token security walls
        token = self.headers.get("X-Wrangler-Token")
        if token != MASTER_TOKEN:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail": "Access Token Denied"}')
            return

        content_length = int(self.headers.get('Content-Length', 0))
        raw_body = self.rfile.read(content_length).decode('utf-8')
        
        lines = raw_body.splitlines()
        clean_lines = [l for l in lines if "," in l and not l.startswith("------")]

        valid_records = []
        errors = []

        for idx, line in enumerate(clean_lines):
            record, error = validate_row(line, idx + 1)


        # Transmit valid student records straight to Supabase REST engine
        if valid_records and SUPABASE_KEY:
            # Append the table path to the base URL!
            target_url = f"{SUPABASE_URL}/rest/v1/sasams_students"
            
            # This print statement will show up in Render logs for debugging
            print(f"DEBUG: Attempting to POST to {target_url}")

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
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"detail": f"Cloud Sync Failed: {str(e)}"}).encode('utf-8'))
                return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response_payload = {
            "status": "completed",
            "valid_count": len(valid_records),
            "data": valid_records,
            "errors": errors
        }
        self.wfile.write(json.dumps(response_payload).encode('utf-8'))

def run():
    # Cloud Run provides the PORT environment variable, fallback to 8000 for local
    port = int(os.environ.get("PORT", 8000))
    server_address = ('0.0.0.0', port)
    httpd = HTTPServer(server_address, DataWranglerServer)
    print(f"The Data Wrangler Native Engine running on port {port}...")
    httpd.serve_forever()

if __name__ == '__main__':
    run()
