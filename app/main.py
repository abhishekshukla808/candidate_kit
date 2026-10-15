"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""

import math
import os
import re
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# ─── Constants ────────────────────────────────────────────────────────────── #
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000
EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SHIFT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# ─── Startup: indexes ─────────────────────────────────────────────────────── #
@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


# ─── Helpers ──────────────────────────────────────────────────────────────── #
def round_half_up(value, decimals=2):
    """Round using ROUND_HALF_UP (not banker's rounding)."""
    d = Decimal(str(value))
    if decimals == 2:
        return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    elif decimals == 4:
        return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    else:
        q = Decimal(10) ** -decimals
        return float(d.quantize(q, rounding=ROUND_HALF_UP))


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC-aware datetime, truncated to whole seconds."""
    seconds = ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def dt_to_epoch_ms(dt: datetime) -> Optional[int]:
    """Convert a datetime to epoch milliseconds. Returns None for None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def validate_epoch_ms(value, field_name="punched_at"):
    """Validate that a value is a valid epoch milliseconds integer."""
    if value is None:
        return None
    if not isinstance(value, int):
        raise HTTPException(422, detail=[{"msg": f"{field_name} must be an integer", "type": "value_error"}])
    if value < EPOCH_MS_MIN or value > EPOCH_MS_MAX:
        raise HTTPException(422, detail=[{"msg": f"{field_name} out of range", "type": "value_error"}])
    return value


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate a datetime to whole seconds."""
    return dt.replace(microsecond=0)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """True if shift_end <= shift_start (overnight shift)."""
    return shift_end <= shift_start


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Compute the attendance date per R1.
    The attendance date is the IST calendar date of punch_in, except for overnight shifts:
    if punch_in IST time is earlier than shift_end, it belongs to the previous day's shift.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    ist_date = punch_in_ist.date()

    if is_overnight_shift(shift_start, shift_end):
        punch_time_str = punch_in_ist.strftime("%H:%M")
        # If the punch time is before shift_end, it belongs to the previous day
        if punch_time_str < shift_end:
            ist_date = ist_date - timedelta(days=1)

    return ist_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    Compute late minutes per R2.
    Grace period is 10 minutes. Late only if punch_in > shift_start + 10:00 (strictly).
    late_minutes = floor(minutes since shift_start).
    """
    h, m = map(int, shift_start.split(":"))
    # Build shift_start datetime in IST on the attendance date
    att_date = date_type.fromisoformat(attendance_date)
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, h, m, 0, tzinfo=IST)

    punch_in_ist = punch_in_utc.astimezone(IST)
    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()

    # Grace period: strictly more than 10 minutes (600 seconds)
    if diff_seconds > 600:
        return int(diff_seconds // 60)  # floor
    return 0


def compute_shift_end_dt(shift_end: str, shift_start: str, attendance_date: str) -> datetime:
    """Compute the shift end datetime in IST, accounting for overnight shifts."""
    h, m = map(int, shift_end.split(":"))
    att_date = date_type.fromisoformat(attendance_date)
    end_date = att_date
    if is_overnight_shift(shift_start, shift_end):
        end_date = att_date + timedelta(days=1)
    return datetime(end_date.year, end_date.month, end_date.day, h, m, 0, tzinfo=IST)


def compute_overtime(punch_out_utc: datetime, shift_end: str, shift_start: str, attendance_date: str) -> int:
    """
    Compute overtime per R3.
    overtime_minutes = floor(minutes from shift_end to punch_out), only if >= 30.
    """
    shift_end_dt = compute_shift_end_dt(shift_end, shift_start, attendance_date)
    punch_out_ist = punch_out_utc.astimezone(IST)
    diff_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    minutes = int(diff_seconds // 60)  # floor
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """Compute work hours per R4. Half-up rounding to 2 decimals."""
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_half_day(work_hours: float) -> bool:
    """Compute half day per R5. True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def format_employee(doc: dict) -> dict:
    """Format an employee document for the API response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc["created_at"]),
    }


def format_attendance(doc: dict) -> dict:
    """Format an attendance document for the API response."""
    history = doc.get("history") or []
    formatted_history = []
    for entry in history:
        changes = {}
        for field, change in entry.get("changes", {}).items():
            from_val = change.get("from")
            to_val = change.get("to")
            # Convert datetime values to epoch ms for punch_in, punch_out
            if field in ("punch_in", "punch_out"):
                from_val = dt_to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val
                to_val = dt_to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val
            changes[field] = {"from": from_val, "to": to_val}
        formatted_history.append({
            "at": dt_to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": formatted_history,
    }


def count_working_days(start_date: date_type, end_date: date_type) -> int:
    """Count Mon-Fri days between start_date and end_date inclusive."""
    if start_date > end_date:
        return 0
    count = 0
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:  # Mon=0 .. Fri=4
            count += 1
        current += timedelta(days=1)
    return count


def get_month_range(month_str: str):
    """Return (first_day, last_day) as date objects for a YYYY-MM string."""
    year, mon = map(int, month_str.split("-"))
    first = date_type(year, mon, 1)
    if mon == 12:
        last = date_type(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date_type(year, mon + 1, 1) - timedelta(days=1)
    return first, last


def parse_calendar_date(value: str, field_name: str) -> date_type:
    if not DATE_RE.fullmatch(value):
        raise HTTPException(422, detail=[{"msg": f"{field_name} must be YYYY-MM-DD", "type": "value_error"}])
    try:
        return date_type.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, detail=[{"msg": f"Invalid {field_name}", "type": "value_error"}])


def parse_month_range(month: str):
    if not MONTH_RE.fullmatch(month):
        raise HTTPException(422, detail=[{"msg": "month must be YYYY-MM", "type": "value_error"}])
    try:
        return get_month_range(month)
    except ValueError:
        raise HTTPException(422, detail=[{"msg": "Invalid month", "type": "value_error"}])


def build_employee_monthly_pipeline(emp_code: str, date_from: str, date_to: str):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": date_from, "$lte": date_to}}},
        {"$addFields": {
            "day_of_week": {
                "$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
            }
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                    {"$gte": ["$day_of_week", 2]},
                    {"$lte": ["$day_of_week", 6]},
                ]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                "$late_minutes",
                0,
            ]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def build_department_summary_pipeline(date_from: str, date_to: str, department: Optional[str] = None):
    employee_match = {"joined_on": {"$lte": date_to}}
    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", date_from]},
                    {"$lte": ["$date", date_to]},
                ]}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {
            "log_day_of_week": {"$cond": [
                {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date", "format": "%Y-%m-%d"}}},
                0,
            ]},
        }},
        {"$group": {
            "_id": "$emp_code",
            "department": {"$first": "$department"},
            "headcount": {"$first": 1},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": [{"$ifNull": ["$logs.status", ""]}, list(PRESENCE_STATUSES)]},
                    {"$gte": ["$log_day_of_week", 2]},
                    {"$lte": ["$log_day_of_week", 6]},
                ]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1.0]},
                0,
            ]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": [{"$ifNull": ["$logs.status", ""]}, list(PRESENCE_STATUSES)]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]},
                "$logs.work_hours",
                0,
            ]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": [{"$ifNull": ["$logs.status", ""]}, list(PRESENCE_STATUSES)]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]},
                1,
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                "$logs.late_minutes",
                0,
            ]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": "$headcount"},
            "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(date_from: str, date_to: str, limit: int, department: Optional[str] = None):
    pipeline = [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to}, "late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
            "name": {"$first": "$employee.name"},
            "department": {"$first": "$employee.department"},
        }},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": 1,
            "department": 1,
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def build_department_trend_pipeline(department: str, from_date: date_type, to_date: date_type):
    start = datetime(from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc)
    upper_bound = datetime(to_date.year, to_date.month, to_date.day, tzinfo=timezone.utc) + timedelta(days=1)
    return [
        {"$documents": [{"day": start}]},
        {"$densify": {
            "field": "day",
            "range": {"step": 1, "unit": "day", "bounds": [start, upper_bound]},
        }},
        {"$match": {"day": {"$lt": upper_bound}}},
        {"$set": {"day_key": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        {"$lookup": {
            "from": "employees",
            "let": {"day_key": "$day_key"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", department]},
                    {"$lte": ["$joined_on", "$$day_key"]},
                ]}}},
                {"$project": {"_id": 0, "emp_code": 1}},
            ],
            "as": "active_employees",
        }},
        {"$set": {"headcount": {"$size": "$active_employees"}}},
        {"$unwind": {"path": "$active_employees", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day_key": "$day_key", "employee_code": "$active_employees.emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$date", "$$day_key"]},
                    {"$eq": ["$emp_code", "$$employee_code"]},
                ]}}},
            ],
            "as": "log",
        }},
        {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$day_key",
            "day": {"$first": "$day"},
            "headcount": {"$first": "$headcount"},
            "is_working_day": {"$first": {"$and": [
                {"$gte": [{"$dayOfWeek": "$day"}, 2]},
                {"$lte": [{"$dayOfWeek": "$day"}, 6]},
            ]}},
            "present_count": {"$sum": {"$cond": [
                {"$in": [{"$ifNull": ["$log.status", ""]}, list(PRESENCE_STATUSES)]},
                {"$cond": [{"$eq": [{"$ifNull": ["$log.half_day", False]}, True]}, 0.5, 1.0]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]}, 1, 0,
            ]}},
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": [{"$floor": {"$add": [
                    {"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]},
                    0.5,
                ]}}, 10000]},
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "moving_average_raw": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                },
            },
        }},
        {"$set": {"moving_avg_7d": {"$cond": [
            {"$eq": [{"$ifNull": ["$moving_average_raw", None]}, None]},
            None,
            {"$divide": [{"$floor": {"$add": [
                {"$multiply": ["$moving_average_raw", 10000]},
                0.5,
            ]}}, 10000]},
        ]}}},
        {"$project": {
            "_id": 0,
            "date": "$_id",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]


# ─── Pydantic models ─────────────────────────────────────────────────────── #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v):
        if not EMP_CODE_RE.match(v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        if not v or len(v) > 100:
            raise ValueError("name must be 1-100 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        if len(v) > 120 or not EMAIL_RE.match(v):
            raise ValueError("invalid email format")
        return v

    @field_validator("department")
    @classmethod
    def validate_department(cls, v):
        if not v or len(v) > 50:
            raise ValueError("department must be 1-50 characters")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift(cls, v):
        if not SHIFT_RE.match(v):
            raise ValueError("shift must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            date_type.fromisoformat(v)
        except ValueError:
            raise ValueError("invalid date")
        return v

    @model_validator(mode="after")
    def shift_start_ne_end(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {PRESENCE_STATUSES}")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str
    regularized_by: str

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {ALL_STATUSES}")
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v):
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, v):
        if len(v) < 1 or len(v) > 50:
            raise ValueError("regularized_by must be 1-50 characters")
        return v


# ─── Endpoints ────────────────────────────────────────────────────────────── #

# --- Health --- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB not available")


# --- Employees --- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    doc.pop("_id", None)
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_employee(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --- Attendance CRUD --- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        validate_epoch_ms(body.punched_at, "punched_at")
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        ts = truncate_to_seconds(datetime.now(timezone.utc))
    ts = truncate_to_seconds(ts)

    attendance_date = compute_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")
    doc.pop("_id", None)
    return format_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        validate_epoch_ms(body.punched_at, "punched_at")
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        ts = truncate_to_seconds(datetime.now(timezone.utc))
    ts = truncate_to_seconds(ts)

    # Select the latest eligible day first; an older open record must not win over it.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "status": {"$in": list(PRESENCE_STATUSES)},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, detail="No open punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, detail="Already punched out")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validate: punched_at must be > punch_in
    if ts <= punch_in_dt:
        raise HTTPException(422, detail=[{"msg": "punch_out must be after punch_in", "type": "value_error"}])

    # Validate: within 24 hours
    if (ts - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, detail=[{"msg": "punch_out must be within 24 hours of punch_in", "type": "value_error"}])

    work_hrs = compute_work_hours(punch_in_dt, ts)
    ot = compute_overtime(ts, emp["shift_end"], emp["shift_start"], record["date"])
    hd = compute_half_day(work_hrs)

    # Atomically set punch_out (only if still null – handles concurrency)
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hrs,
            "overtime_minutes": ot,
            "half_day": hd,
        }},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="Already punched out")

    result.pop("_id", None)
    return format_attendance(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, detail=[{"msg": f"status must be one of {ALL_STATUSES}", "type": "value_error"}])

    parsed_from = parse_calendar_date(date_from, "date_from") if date_from is not None else None
    parsed_to = parse_calendar_date(date_to, "date_to") if date_to is not None else None
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise HTTPException(422, detail=[{"msg": "date_from must be <= date_to", "type": "value_error"}])

    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if parsed_from or parsed_to:
        q["date"] = {}
        if parsed_from:
            q["date"]["$gte"] = parsed_from.isoformat()
        if parsed_to:
            q["date"]["$lte"] = parsed_to.isoformat()
    if status is not None:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_attendance(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --- Regularize --- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parsed_date = parse_calendar_date(date, "date")
    date = parsed_date.isoformat()
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="No record for this date")

    new_status = body.status if body.status is not None else record["status"]
    is_absence = new_status in ("ABSENT", "LEAVE")

    # Validate: ABSENT/LEAVE with punch times → 422
    if is_absence and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, detail=[{"msg": "Cannot supply punch times with ABSENT/LEAVE status", "type": "value_error"}])

    # Build new values
    if is_absence:
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            validate_epoch_ms(body.punch_in, "punch_in")
            new_punch_in = truncate_to_seconds(epoch_ms_to_dt(body.punch_in))
        else:
            new_punch_in = record.get("punch_in")
            if new_punch_in is not None and new_punch_in.tzinfo is None:
                new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

        if new_punch_in is None:
            raise HTTPException(422, detail=[{"msg": "Presence status requires a punch_in", "type": "value_error"}])

        if body.punch_out is not None:
            validate_epoch_ms(body.punch_out, "punch_out")
            new_punch_out = truncate_to_seconds(epoch_ms_to_dt(body.punch_out))
        else:
            new_punch_out = record.get("punch_out")
            if new_punch_out is not None and new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)

        # Validate punch_in stays on the record's attendance date (R1)
        computed_date = compute_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(422, detail=[{"msg": "punch_in does not match the record's attendance date", "type": "value_error"}])

        # Validate punch_out > punch_in and within 24h
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, detail=[{"msg": "punch_out must be after punch_in", "type": "value_error"}])
            if (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(422, detail=[{"msg": "punch_out must be within 24 hours of punch_in", "type": "value_error"}])

    # Compute derived fields
    if is_absence:
        new_late = 0
        new_work_hours = None
        new_overtime = 0
        new_half_day = False
    else:
        new_late = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime = compute_overtime(new_punch_out, emp["shift_end"], emp["shift_start"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime = 0
            new_half_day = False

    # Build changes dict
    old_punch_in = record.get("punch_in")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    old_punch_out = record.get("punch_out")
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)
    old_status = record["status"]
    old_late = record.get("late_minutes", 0)
    old_work_hours = record.get("work_hours")
    old_overtime = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_overtime != old_overtime:
        changes["overtime_minutes"] = {"from": old_overtime, "to": new_overtime}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(422, detail=[{"msg": "No changes detected", "type": "value_error"}])

    now = truncate_to_seconds(datetime.now(timezone.utc))
    history_entry = {
        "at": now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    update_set = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }

    # Concurrent safety: match on history length so a concurrent edit fails
    old_history = record.get("history") or []
    history_match = {"history": old_history}
    if not record.get("history"):
        history_match = {"$or": [{"history": []}, {"history": {"$exists": False}}]}
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], **history_match},
        {
            "$set": update_set,
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="Concurrent edit conflict")

    result.pop("_id", None)
    return format_attendance(result)


# ─── Analytics ────────────────────────────────────────────────────────────── #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    first_day, last_day = parse_month_range(month)
    joined = date_type.fromisoformat(emp["joined_on"])
    effective_start = max(first_day, joined)
    working_days = count_working_days(effective_start, last_day) if effective_start <= last_day else 0
    pipeline = build_employee_monthly_pipeline(emp_code, first_day.isoformat(), last_day.isoformat())
    result = list(db.attendance_logs.aggregate(pipeline))
    data = result[0] if result else {}
    present_days = data.get("present_days", 0)
    attendance_pct = round_half_up(present_days / working_days * 100, 4) if working_days else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": data.get("leave_days", 0),
        "late_count": data.get("late_count", 0),
        "total_late_minutes": data.get("total_late_minutes", 0),
        "total_overtime_minutes": data.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first_day, last_day = parse_month_range(month)
    pipeline = build_department_summary_pipeline(first_day.isoformat(), last_day.isoformat(), department)
    results = list(db.employees.aggregate(pipeline))
    items = []
    for row in results:
        work_hours_count = row.get("work_hours_count", 0)
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": round_half_up(row["present_days"], 2),
            "avg_work_hours": round_half_up(row["work_hours_sum"] / work_hours_count, 2) if work_hours_count else None,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_day, last_day = parse_month_range(month)
    pipeline = build_late_leaderboard_pipeline(
        first_day.isoformat(), last_day.isoformat(), limit, department
    )
    results = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": results}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    # Validate department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, detail="Department not found")

    from_d = parse_calendar_date(from_date, "from")
    to_d = parse_calendar_date(to_date, "to")

    if to_d < from_d:
        raise HTTPException(422, detail=[{"msg": "to must be >= from", "type": "value_error"}])

    if (to_d - from_d).days > 92:
        raise HTTPException(422, detail=[{"msg": "Range must not exceed 92 days", "type": "value_error"}])

    pipeline = build_department_trend_pipeline(department, from_d, to_d)
    items = list(db.aggregate(pipeline, allowDiskUse=True))
    return {"department": department, "items": items}


# ─── Admin: Explain ──────────────────────────────────────────────────────── #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = {
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(422, detail=[{"msg": f"endpoint must be one of {valid_endpoints}", "type": "value_error"}])

    if endpoint == "attendance_list":
        if status is not None and status not in ALL_STATUSES:
            raise HTTPException(422, detail=[{"msg": "Invalid status", "type": "value_error"}])
        parsed_from = parse_calendar_date(date_from, "date_from") if date_from else None
        parsed_to = parse_calendar_date(date_to, "date_to") if date_to else None
        if parsed_from and parsed_to and parsed_from > parsed_to:
            raise HTTPException(422, detail=[{"msg": "date_from must be <= date_to", "type": "value_error"}])
        q = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if parsed_from or parsed_to:
            q["date"] = {}
            if parsed_from:
                q["date"]["$gte"] = parsed_from.isoformat()
            if parsed_to:
                q["date"]["$lte"] = parsed_to.isoformat()
        if status is not None:
            q["status"] = status
        skip = (page - 1) * page_size

        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "projection": {"_id": 0},
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail=[{"msg": "emp_code and month required", "type": "value_error"}])
        first_day, last_day = parse_month_range(month)
        pipeline = build_employee_monthly_pipeline(emp_code, first_day.isoformat(), last_day.isoformat())
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail=[{"msg": "month required", "type": "value_error"}])
        first_day, last_day = parse_month_range(month)
        pipeline = build_department_summary_pipeline(first_day.isoformat(), last_day.isoformat(), department)
        explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}, "allowDiskUse": True},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail=[{"msg": "month required", "type": "value_error"}])
        first_day, last_day = parse_month_range(month)
        pipeline = build_late_leaderboard_pipeline(
            first_day.isoformat(), last_day.isoformat(), limit, department
        )
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, detail=[{"msg": "department, from, to required", "type": "value_error"}])
        if not db.employees.find_one({"department": department}, {"_id": 1}):
            raise HTTPException(404, detail="Department not found")
        from_d = parse_calendar_date(from_date, "from")
        to_d = parse_calendar_date(to_date, "to")
        if to_d < from_d or (to_d - from_d).days > 92:
            raise HTTPException(422, detail=[{"msg": "Invalid trend date range", "type": "value_error"}])
        pipeline = build_department_trend_pipeline(department, from_d, to_d)
        explain = db.command(
            "explain",
            {"aggregate": 1, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "database", "explain": explain}
