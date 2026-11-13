import os
import re
import json
import cgi
import io
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.error

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://reqzqlzphirvqoaycbkl.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
MASTER_TOKEN = os.getenv("WRANGLER_TOKEN", "WranglerSecureToken2026_NC")

# 1. Security: Limit max upload size to 5MB to prevent DoS
MAX_UPLOAD_SIZE = 5 * 1024 * 1024 

def sanitize_string(v: str) -> str:
    clean = re.sub(r"<[^>]*>", "", str(v))
    clean = re.sub(r"[;'\"]", "", clean)
    return clean.strip()

def validate_row(line: str, index: int):
    fields = [f.strip() for f in line.split(",")]
    if len(fields) != 6:
        return None, f"Line {index}: Expected 6 columns."
    
    id_num, first, last, grade, sba, att = fields
    
    if not re.match(r"^\d{13}$", id_num):
        return None, f"Line {index}: ID must be 13 digits."
        
    try:
        grade_int = int(grade)
        if not (1 <= grade_int <= 12):
            return None, f"Line {index}: Grade 1-12."
            
        sba_float = float(sba) if sba else None
        if sba_float is not None and not (0.0 <= sba_float <= 100.0):
            return None, f"Line {index}: SBA 0-100."
            
        att_float = float(att)
        if not (0.0 <= att_float <= 1.0):
            return None, f"Line {index}: Attendance 0.0-1.0."
    except ValueError:
        return None, f"Line {index}: Numeric parsing failed."

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

        token = self.headers.get("X-Wrangler-Token")
        if token != MASTER_TOKEN:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"detail": "Access Token Denied"}')
            return

        content_length = int(self.headers.get('Content-Length', 0))
        
        # Security Check: Block huge payloads
        if content_length > MAX_UPLOAD_SIZE:
            self.send_response(413) # Payload Too Large
            self.end_headers()
            self.wfile.write(b'{"detail": "File exceeds 5MB limit."}')
            return

        raw_body = self.rfile.read(content_length)
        
        # Parse multipart/form-data (File Upload)
        raw_lines = []
        try:
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' in content_type:
                # Standard library way to parse file uploads
                environ = {'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': content_type, 'CONTENT_LENGTH': str(content_length)}
                fs = cgi.FieldStorage(fp=io.BytesIO(raw_body), environ=environ, keep_blank_values=True)
                
                for field in fs.list:
                    if field.filename: # Found the uploaded file
                        file_content = field.file.read().decode('utf-8', errors='ignore')
                        raw_lines = file_content.splitlines()
                        break
            else:
                # Fallback for raw CSV text (our previous tests)
                raw_lines = raw_body.decode('utf-8').splitlines()
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(json.dumps({"detail": f"Parse error: {str(e)}"}).encode('utf-8'))
            return

        # Filter out empty lines or SA-SAMS header/footer junk
        clean_lines = [l for l in raw_lines if "," in l and not l.startswith("------")]
        
        valid_records = []
        errors = []

        for idx, line in enumerate(clean_lines):
            record, error = validate_row(line, idx + 1)
            if record:
                valid_records.append(record)
            else:
                errors.append({"error": error})

        # 2. Transmit valid student records straight to Supabase REST engine
        if valid_records and SUPABASE_KEY:
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
                self.end_headers()
                self.wfile.write(json.dumps({"detail": f"Supabase Error: {error_body}"}).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"detail": str(e)}).encode('utf-8'))
                return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "status": "completed",
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