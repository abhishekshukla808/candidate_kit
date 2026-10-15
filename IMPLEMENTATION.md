# Implementation Guide: Employee Attendance & Analytics API

This document provides a comprehensive technical walkthrough of how the Employee Attendance & Analytics API was designed, structured, and implemented in [`app/main.py`](file:///c:/Users/shukl/OneDrive/Documents/SDT_candidate_kit/candidate_kit/app/main.py) in accordance with [`openapi.yaml`](file:///c:/Users/shukl/OneDrive/Documents/SDT_candidate_kit/candidate_kit/openapi.yaml), [`DATA_MODEL.md`](file:///c:/Users/shukl/OneDrive/Documents/SDT_candidate_kit/candidate_kit/DATA_MODEL.md), and the problem statement requirements.

---

## 1. Architectural Overview & Design Constraints

### Core Requirements
- **Single-File Implementation**: All application logic, database helpers, validation schemas, routing, and aggregations reside in [`app/main.py`](file:///c:/Users/shukl/OneDrive/Documents/SDT_candidate_kit/candidate_kit/app/main.py).
- **Startup Command**: Starts via `uvicorn app.main:app --port 8000`.
- **Environment Configuration**: Dynamic reading of `MONGO_URI` (default `mongodb://localhost:27017`) and `MONGO_DB` (default `attendance_db`) loaded via `python-dotenv`.
- **API Boundary & Types**:
  - **Instants**: Transferred over HTTP as integer epoch **milliseconds** (`100000000000` to `4102444800000`), stored in MongoDB as BSON dates (`datetime.datetime` with UTC).
  - **Identifiers**: Employees are identified everywhere by the business code `emp_code` (e.g. `EMP0001`). MongoDB's internal `_id` is never accepted or returned in requests/responses.
  - **Calendar Dates / Times**: Plain strings `YYYY-MM-DD` and `HH:MM` (24-hour, IST).
  - **Sub-second Truncation**: Instants are truncated to whole seconds prior to any business rule calculations or database persistence.

---

## 2. Database Schema & Startup Indexes

Indexes are automatically and idempotently provisioned during FastAPI's `@app.on_event("startup")` lifecycle hook:

```python
@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
```

### Why each index was chosen:
1. **`employees.emp_code` (Unique)**:
   - Primary business identifier lookup.
   - Enforces unique employee creation at the database engine level (race-safe).
2. **`employees.department`**:
   - Accelerates department filtering on `GET /employees` and cross-collection joins.
3. **`attendance_logs.(emp_code, date)` (Unique Compound)**:
   - Enforces the natural invariant of at most one attendance record per employee per calendar date.
   - Converts concurrent duplicate punch-ins into an immediate, atomic `DuplicateKeyError` (409 Conflict).
4. **`attendance_logs.(date DESC, emp_code ASC)`**:
   - Matches the exact sorting requirements of `GET /attendance`, eliminating memory sorting on ~100k records (`IXSCAN` plan without `COLLSCAN`).
4. **`attendance_logs.(emp_code, punch_in DESC)`**:
  - Finds the latest eligible punch-in efficiently for punch-out.
5. **`employees.department, joined_on` and `employees.joined_on`**:
  - Support department-scoped and all-department active-headcount queries, including trend lookups.

## 3. Business Rules (R1 - R10) Implementation

### R1: Timezone & Attendance Date (with Overnight Shifts)
- **Timezone**: IST is fixed as UTC+05:30: `timezone(timedelta(hours=5, minutes=30))`.
- **Date Derivation**: An attendance record's `date` is the IST calendar date of `punch_in`.
- **Overnight Shifts (`shift_end <= shift_start`)**:
  - If an overnight shift employee punches in at an IST time **strictly earlier than `shift_end`**, the punch belongs to the shift that started on the previous calendar day.
  - Implemented in `compute_attendance_date()`:
    ```python
    if is_overnight_shift(shift_start, shift_end):
        punch_time_str = punch_in_ist.strftime("%H:%M")
        if punch_time_str < shift_end:
            ist_date = ist_date - timedelta(days=1)
    ```

### R2: Late Minutes & Grace Period
- **Grace Period**: 10 minutes from `shift_start`.
- **Strict Cutoff**: Late **only if** `punch_in > shift_start + 10:00` (strictly greater than 600 seconds).
- **Floor Calculation**: When late, minutes elapsed is calculated from `shift_start` (not the end of grace period) using floor division:
  ```python
  diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()
  if diff_seconds > 600:
      return int(diff_seconds // 60)
  return 0
  ```

### R3: Overtime Minutes
- Calculated as floor minutes elapsed between `shift_end` and `punch_out`.
- For overnight shifts, `shift_end` falls on the **next calendar day** relative to the record's `date`.
- Only credited if elapsed time is $\ge 30$ minutes; otherwise $0$.

### R4: Work Hours & Half-Up Rounding
- Calculated as `(punch_out - punch_in).total_seconds() / 3600`.
- Python's built-in `round()` uses Banker's rounding (round-half-to-even), which produces inaccurate figures like `round(8.125, 2) -> 8.12`.
- Standard decimal half-up rounding is implemented via Python's `decimal` module:
  ```python
  def round_half_up(value, decimals=2):
      d = Decimal(str(value))
      q = Decimal("0.01") if decimals == 2 else Decimal("0.0001")
      return float(d.quantize(q, rounding=ROUND_HALF_UP))
  ```

### R5: Half Day
- Computed at punch-out and regularization: `half_day = True` when rounded `work_hours < 4.50`.
- Counts as $0.5$ of a present day in attendance analytics.

### R6 & R7: Presence & Working Days
- **Presence**: `PRESENT`, `WFH`, and `ON_DUTY` count as present. `ABSENT` and `LEAVE` do not.
- **Working Days**: Monday through Friday (no holidays).
- **Mid-Month Joiners**: Counted from `max(month_start, joined_on)` up to `month_end`.
- Weekend records add to overtime and late totals, but do not increment `present_days` toward working-day presence percentages.

### R8 & R9: Rounding & Headcount
- **Rounding**: Standard metrics rounded to 2 decimals half-up; rates rounded to 4 decimals half-up.
- **Headcount**: All employees with `joined_on <= period_end` are counted, **regardless of whether they have logged any attendance records**.

### R10: Pagination
- 1-indexed pagination (`page >= 1`).
- `page_size` constrained to `1 <= page_size <= 100` (default 20).
- `skip = (page - 1) * page_size`.
- `total` strictly reflects the filtered set using `count_documents(q)`.

---

## 4. Endpoint Implementation Details

### 4.1 System & Employee Endpoints
- **`GET /health`**:
  Executes `client.admin.command("ping")`. Returns `{"status": "ok"}` on 200, raises `503 Service Unavailable` if MongoDB is unreachable.
- **`POST /employees`**:
  Validates `emp_code` regex (`^EMP\d{4,6}$`), email format, string lengths, and ensures `shift_start != shift_end`. Persists `created_at` as UTC datetime. Relies on the unique index on `emp_code` to catch duplicate creates and emit `409 Conflict`.
- **`GET /employees`**:
  Supports exact department filtering, sorts by `emp_code` ascending, and returns paginated employees with `created_at` formatted as epoch milliseconds.

### 4.2 Attendance Lifecycle & Concurrency
- **`POST /attendance/punch-in`**:
  1. Validates employee existence (returns 404 if missing).
  2. Parses or defaults `punched_at`, truncates to whole seconds.
  3. Computes attendance date via R1 and late minutes via R2.
  4. Inserts document with initial defaults (`punch_out: None`, `work_hours: None`, `overtime_minutes: 0`, `half_day: False`, `history: []`).
  5. Catches `DuplicateKeyError` on `(emp_code, date)` and returns `409 Conflict` (guaranteeing race safety).
- **`POST /attendance/punch-out`**:
  1. Locates the most recent unclosed presence record for the employee where `punch_in <= punched_at`.
  2. Validates that `punch_out > punch_in` and is within 24 hours (returns 422 on breach).
  3. Computes `work_hours`, `overtime_minutes`, and `half_day`.
  4. Uses an atomic `find_one_and_update` matching `{"_id": record["_id"], "punch_out": None}` to close the record. If another worker closed it concurrently, returns `409 Conflict`.
- **`GET /attendance`**:
  Index-backed streaming query supporting `emp_code`, `date_from`, `date_to`, and `status` filters. Validates `date_from <= date_to`. Sorts by `date DESC, emp_code ASC`.
- **`PATCH /attendance/{emp_code}/{date}` (Regularization)**:
  1. Verifies that `ABSENT` or `LEAVE` cannot be submitted alongside punch times.
  2. Ensures presence status retains or receives a valid `punch_in`.
  3. Verifies that modified `punch_in` remains within the original record's attendance date.
  4. Recomputes derived fields (`late_minutes`, `work_hours`, `overtime_minutes`, `half_day`).
  5. Assembles audit trail entry in `history` recording only changed fields.
  6. Employs optimistic concurrency control: updates with `{"history": {"$size": len(old_history)}}`. If a concurrent edit appended history first, returns `409 Conflict`.

### 4.3 Analytics Aggregation Pipelines
- **`GET /analytics/employees/{emp_code}/monthly`**:
  - Calculates working days in Python based on `joined_on` and calendar month boundaries.
  - Uses a MongoDB `$facet` aggregation over `attendance_logs` to calculate:
    - `present_days`: MongoDB `$dayOfWeek` restricts to Monday–Friday (2 to 6), crediting $0.5$ for half days.
    - `leave_days`: Total `LEAVE` records.
    - `late_count` & `total_late_minutes`: Aggregated over records where `late_minutes > 0`.
    - `total_overtime_minutes`: Sum of `overtime_minutes`.
- **`GET /analytics/departments/summary`**:
  - Starts from employees joined on or before the month's end and looks up each employee's monthly logs using the `(emp_code, date)` index. Empty lookups are preserved, so employees with no logs still count toward headcount.
  - Groups employee and record metrics by department:
    - Computes `avg_work_hours` across presence records with non-null `work_hours` (record-level average, not average of averages).
    - Aggregates `present_days`, `late_count`, `total_late_minutes`, `leave_count`, and `on_duty_count`.
- **`GET /analytics/leaderboard/late`**:
  - Groups attendance records by `emp_code` where `late_minutes > 0` within the month.
  - Implements **standard competition ranking** via `$setWindowFields`:
    ```json
    {
      "$setWindowFields": {
        "sortBy": { "total_late_minutes": -1, "_id": 1 },
        "output": { "rank": { "$rank": {} } }
      }
    }
    ```
  - Ties share identical ranks with subsequent ranks skipped (e.g. 1, 2, 2, 4).
  - The query `limit` filter (`$match: { rank: { $lte: limit } }`) is applied **after ranking**, correctly retaining all tied candidates at the cutoff boundary.
- **`GET /analytics/departments/{department}/trend`**:
  - Validates department existence and date boundaries (at most 92 days apart).
  - Uses `$documents` and `$densify` to produce one database row per date, then indexed lookups for employees active by that day and their records.
  - Computes daily attendance metrics and the preceding seven-row non-null average with `$setWindowFields`; no records or dates are fetched for Python-side looping.

### 4.4 Admin Query Profiling
- **`GET /admin/explain/{endpoint}`**:
  - Reuses the query and aggregation builders used by the named endpoint, including filters, sort and pagination where applicable.
  - Calls `db.command("explain", ..., verbosity="executionStats")` and returns the raw plan.
  - Proves index coverage (`IXSCAN`) and absence of full collection scans (`COLLSCAN`) across 100k+ records.

---

## 5. Review & Verification

### Fixes from Starter Code
A total of **34 defects** are cataloged in [`REVIEW.md`](file:///c:/Users/shukl/OneDrive/Documents/SDT_candidate_kit/candidate_kit/REVIEW.md), spanning:
- Mock health checks replaced with real database pings.
- Race conditions eliminated through atomic DB operations and unique indexes.
- Timezone discrepancies corrected with explicit UTC instants and IST shifts.
- Corrected pagination math (`(page - 1) * page_size`) and dynamic filtering totals.
- Replaced banker's rounding with true decimal half-up rounding.
- Fixed overnight shift boundaries and overtime grace periods.

### Running & Testing Locally
```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Seed sample dataset
python sample_seed.py

# 3. Start development server
uvicorn app.main:app --port 8000 --reload
```
