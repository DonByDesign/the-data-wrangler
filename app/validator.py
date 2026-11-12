import re
from typing import List, Optional, Dict, Any

def validate_and_sanitize_record(fields: List[str]) -> Dict[str, Any]:
    """Validates and sanitizes a single row of SA-SAMS data using pure Python."""
    
    # 1. Validate ID Number (13 digits, strip whitespace)
    id_number = re.sub(r"\s+", "", str(fields[0]))
    if not re.match(r"^\d{13}$", id_number):
        raise ValueError("ID must be exactly 13 digits. Blocked potential exploitation payload.")

    # 2. Sanitize Names (Strip XSS/HTML and SQLi characters)
    def sanitize_name(name_str: str, field_name: str) -> str:
        clean = re.sub(r"<[^>]*>", "", str(name_str))      # Strip HTML tags
        clean = re.sub(r"[;'\"]", "", clean)               # Strip SQL injection quotes
        clean = clean.strip()
        if not (2 <= len(clean) <= 50):
            raise ValueError(f"{field_name} must be between 2 and 50 characters.")
        return clean

    first_name = sanitize_name(fields[1], "First name")
    last_name = sanitize_name(fields[2], "Last name")

    # 3. Validate Grade (1 to 12)
    grade = int(fields[3])
    if not (1 <= grade <= 12):
        raise ValueError("Grade must be between 1 and 12.")

    # 4. Validate SBA Mark (0 to 100, Optional)
    sba_mark = None
    if fields[4]:
        sba_mark = float(fields[4])
        if not (0.0 <= sba_mark <= 100.0):
            raise ValueError("SBA mark must be between 0 and 100.")

    # 5. Validate Attendance Rate (0.0 to 1.0)
    attendance_rate = float(fields[5])
    if not (0.0 <= attendance_rate <= 1.0):
        raise ValueError("Attendance rate must be between 0.0 and 1.0.")

    # Return the clean dictionary
    return {
        "id_number": id_number,
        "first_name": first_name,
        "last_name": last_name,
        "grade": grade,
        "sba_mark": sba_mark,
        "attendance_rate": attendance_rate
    }

def wrangle_raw_payload(raw_lines: List[str]) -> Dict[str, Any]:
    validated_records = []
    error_logs = []

    for index, line in enumerate(raw_lines):
        if not line.strip():
            continue
            
        fields = [f.strip() for f in line.split(",")]
        
        if len(fields) != 6:
            error_logs.append({"line": index + 1, "error": f"Format Mismatch: Expected 6 parameters, got {len(fields)}"})
            continue

        try:
            record = validate_and_sanitize_record(fields)
            validated_records.append(record)
        except Exception as e:
            error_logs.append({"line": index + 1, "error": str(e)})

    return {
        "status": "completed",
        "valid_count": len(validated_records),
        "data": validated_records,
        "errors": error_logs
    }