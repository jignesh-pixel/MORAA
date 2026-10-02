# 💎 MORAA GemVision — Backend

FastAPI-powered REST API for the MORAA GemVision jewellery analysis platform.

## ✨ Features

- JWT-based authentication (signup / login / token refresh)
- Image upload with validation (type & size)
- Pluggable AI analysis engines (mock / vision)
- Celery task queue for async analysis (eager mode for dev)
- PDF report generation (ReportLab)
- Analysis history with pagination
- Database migrations (Alembic)
- Rate limiting, CORS, structured logging (Loguru)

---

## 🚀 How to Run the Backend

### Prerequisites

- **Python** >= 3.11
- **Redis** (optional — only if you disable eager task mode)
- **PostgreSQL** (optional — SQLite works out of the box)

### Step-by-Step Setup

#### 1. Clone & navigate to the backend

```bash
cd backend
```

#### 2. Create a virtual environment

```bash
python -m venv venv
```

**Activate it:**

| Platform | Command |
|---|---|
| **Linux / Mac** | `source venv/bin/activate` |
| **Windows (CMD)** | `venv\Scripts\activate` |
| **Windows (PowerShell)** | `.\venv\Scripts\Activate.ps1` |

You should see `(venv)` appear in your terminal prompt.

#### 3. Install dependencies

```bash
pip install -r requirements-dev.txt -c constraints.txt
```

#### 4. Configure environment

Create a `backend/.env` file with the following minimum content:

```env
# Use SQLite for development (no external DB needed)
DATABASE_URL=sqlite:///./data/moraa_gemvision.db
SECRET_KEY=my-dev-secret-key
AI_ENGINE_TYPE=mock
```

For a full example, see the [root README](../README.md#-example-env-backend).

#### 5. Run database migrations

```bash
alembic upgrade head
```

This creates the SQLite database file at `backend/data/moraa_gemvision.db` with all required tables.

#### 6. Start the API server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

You should see output like:

```
INFO:     Uvicorn running on http://0.0.0.0:8000
INFO:     Application startup complete.
```

#### 7. Verify it's working

Open these URLs in your browser:

| URL | What to expect |
|---|---|
| `http://localhost:8000/` | JSON with app name, version, links |
| `http://localhost:8000/health` | `{"status": "healthy"}` |
| `http://localhost:8000/docs` | Interactive Swagger API docs |
| `http://localhost:8000/redoc` | Alternative ReDoc API docs |

---

## 🔁 Running with the Frontend

The frontend (Next.js on port 3000) needs the backend running on port 8000.

**Option A — Two terminals:**

| Terminal 1 (Backend) | Terminal 2 (Frontend) |
|---|---|
| `cd backend` | `cd frontend` |
| `source venv/bin/activate` | `npm run dev` |
| `uvicorn app.main:app --reload --port 8000` | |

**Option B — Background process (Linux/Mac):**

```bash
cd backend
source venv/bin/activate
nohup uvicorn app.main:app --reload --port 8000 > server.log 2>&1 &
cd ../frontend
npm run dev
```

---

## 📬 Running Celery Workers (Optional)

If you have Redis running and want asynchronous task processing:

```bash
# Terminal 1 — Start the worker
cd backend
source venv/bin/activate
celery -A celery_worker worker -l info -Q analysis --autoreload

# Terminal 2 — Start the API server
cd backend
source venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

> **Dev mode default:** `CELERY_TASK_ALWAYS_EAGER=true` runs tasks synchronously — no Redis required. Set to `false` in production.

---

## 🧪 Running Tests

```bash
cd backend
python -m pytest
```

Run tests with verbose output:

```bash
python -m pytest -v
```

---

## 🐘 Database

- **Default:** SQLite (`./data/moraa_gemvision.db`) — zero config, ideal for development
- **Production:** PostgreSQL — set `DATABASE_URL` in `.env`

### Migrations

```bash
# Auto-generate a new migration
alembic revision --autogenerate -m "description"

# Apply pending migrations
alembic upgrade head

# Rollback one step
alembic downgrade -1

# Check current migration status
alembic current
```

---

## 🧠 AI Engines

Engines are loaded via the factory in `app/ai/engine_factory.py`.

| Engine | Description |
|---|---|
| `mock` | Random results — great for frontend dev |
| `vision` | PIL-based real image analysis |

Switch with `AI_ENGINE_TYPE=mock` or `AI_ENGINE_TYPE=vision` in `.env`.

---

## 📂 Project Structure

```
backend/
├── app/
│   ├── ai/              # AI analysis engines
│   ├── api/routes/      # FastAPI route handlers
│   ├── middleware/       # CORS, logging, rate limit, error handlers
│   ├── models/          # SQLAlchemy ORM models
│   ├── schemas/         # Pydantic request/response schemas
│   ├── services/        # Business logic layer
│   ├── tasks/           # Celery async tasks
│   ├── repositories/    # Data access layer
│   ├── utils/           # Helpers (file handling, security, logging)
│   ├── config.py        # Settings via Pydantic
│   ├── database.py      # DB engine & session
│   ├── celery_app.py    # Celery app instance
│   └── main.py          # FastAPI entry point
├── alembic/             # Database migrations
├── uploads/             # Uploaded images
├── reports/             # Generated PDF reports
├── .env                 # Environment variables
└── requirements.txt
```
