import re
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator

class StudentRecord(BaseModel):
    id_number: str = Field(..., description="South African National ID or Passport")
    first_name: str = Field(..., min_length=2, max_length=50)
    last_name: str = Field(..., min_length=2, max_length=50)
    grade: int = Field(..., ge=1, le=12)
    sba_mark: Optional[float] = Field(None, ge=0.0, le=100.0)
    attendance_rate: float = Field(..., ge=0.0, le=1.0)

    @field_validator("id_number", mode="before")
    @classmethod
    def validate_id_number(cls, v: str) -> str:
        clean_id = re.sub(r"\s+", "", str(v))
        if not re.match(r"^\d{13}$", clean_id):
            raise ValueError("ID must be exactly 13 digits. Blocked potential exploitation payload.")
        return clean_id

    @field_validator("first_name", "last_name", mode="before")
    @classmethod
    def sanitize_string(cls, v: str) -> str:
        clean = re.sub(r"<[^>]*>", "", str(v))
        clean = re.sub(r"[;'\"]", "", clean)
        return clean.strip()

def wrangle_raw_payload(raw_lines: List[str]):
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
            record = StudentRecord(
                id_number=fields[0],
                first_name=fields[1],
                last_name=fields[2],
                grade=int(fields[3]),
                sba_mark=float(fields[4]) if fields[4] else None,
                attendance_rate=float(fields[5])
            )
            validated_records.append(record.model_dump())
        except Exception as e:
            error_logs.append({"line": index + 1, "error": str(e)})

    return {
        "status": "completed",
        "valid_count": len(validated_records),
        "data": validated_records,
        "errors": error_logs
    }
