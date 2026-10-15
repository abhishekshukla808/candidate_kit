# Employee Attendance & Analytics API

A high-performance, production-grade attendance logging and analytics service built with FastAPI and MongoDB, conforming strictly to the OpenAPI 3.0 specification (openapi.yaml) and business rules R1–R10.

All application logic, validation schemas, database operations, and MongoDB aggregation pipelines reside in a single file: app/main.py.

Tech Stack

Framework: FastAPI (Python 3.12+)

Server: Uvicorn

Database: MongoDB 

Driver: PyMongo

Data Validation: Pydantic v2


## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

## Documentation
- Detailed implementation breakdown: [`IMPLEMENTATION.md`](./IMPLEMENTATION.md)
- Defect review of starter code: [`REVIEW.md`](./REVIEW.md)
- Architectural & design decisions: [`DECISIONS.md`](./DECISIONS.md)

Project Structure

├── app/
│   ├── __init__.py
│   └── main.py              # Complete API implementation (all endpoints & aggregations)
├── sample_data/
│   ├── employees.json        # Extended JSON sample employees
│   └── attendance_logs.json  # Extended JSON sample attendance logs
├── .env.example             # Environment configuration template
├── DATA_MODEL.md            # MongoDB collection and document specification
├── DECISIONS.md             # Key architectural & engineering design decisions
├── IMPLEMENTATION.md        # In-depth technical walkthrough of rules and pipelines
├── openapi.yaml             # Complete OpenAPI 3.0 API contract
├── requirements.txt         # Project dependencies
├── REVIEW.md                # Detailed log of 26 defects identified & fixed from starter
├── sample_seed.py           # Database seeder script
└── README.md                # Project guide & documentation

