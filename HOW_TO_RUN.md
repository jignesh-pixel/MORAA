# 💎 MORAA GemVision — How to Run from Scratch

> **Complete guide to set up and run the MORAA GemVision jewellery analysis platform on your local machine.**

---

## 1. Project Overview

**MORAA GemVision** is a full-stack AI-powered jewellery analysis platform. Users upload photos of gemstones or jewellery, and the platform evaluates quality, identifies characteristics, detects potential flaws, and generates professional PDF reports.

### Tech Stack

| Layer | Technology |
|---|---|
| **Frontend** | Next.js 16 + React 19 + TypeScript |
| **Styling** | Tailwind CSS v4 + Framer Motion (animations) |
| **State Management** | Zustand |
| **Backend API** | FastAPI (Python 3.11+) |
| **Database** | SQLite (default dev) / PostgreSQL (production) |
| **ORM** | SQLAlchemy 2.0 + Alembic (migrations) |
| **Task Queue** | Celery + Redis (optional, eager mode by default) |
| **AI Engines** | Mock (random) / Vision (PIL-based) / Gemini API (optional) |
| **Authentication** | JWT (python-jose) |
| **PDF Reports** | ReportLab |

### Required Software

- **Node.js** >= 18 (recommended: 20 LTS)
- **Python** >= 3.11
- **Git** (to clone the repository)
- **Redis** (optional — only if you disable eager Celery mode)

---

## 2. Prerequisites

Before you begin, ensure you have the following installed:

| Software | Minimum Version | Check Command |
|---|---|---|
| Node.js | 18.x | `node --version` |
| npm | 9.x | `npm --version` |
| Python | 3.11 | `python --version` |
| pip | (comes with Python) | `pip --version` |
| Git | any recent version | `git --version` |

> **Optional but recommended:** [Redis](https://redis.io/download/) if you want to run Celery workers asynchronously.

---

## 3. Clone the Repository

```bash
git clone <repository-url>
cd moraa-gemvision
```

This gives you the following top-level structure:

```
moraa-gemvision/
├── frontend/          # Next.js 16 web application
├── backend/           # FastAPI REST API
├── ai-engine/         # (coming soon)
├── shared/            # (coming soon)
├── docs/              # (coming soon)
├── docker/            # (coming soon)
├── package.json       # Root package (minimal)
└── README.md
```

---

## 4. Backend Setup

> **All backend commands must be run from the `backend/` directory.**

### 4.1 Navigate to Backend

```bash
cd backend
```

### 4.2 Create a Python Virtual Environment

**Windows (Command Prompt):**
```bash
python -m venv venv
venv\Scripts\activate
```

**Windows (PowerShell):**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**Linux / macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
```

> ✅ You should see `(venv)` appear in your terminal prompt when the virtual environment is active.

### 4.3 Install Backend Dependencies

```bash
pip install -r requirements.txt
```

This installs all packages listed in `backend/requirements.txt`, including:
- FastAPI, Uvicorn (web server)
- SQLAlchemy, Alembic (database ORM & migrations)
- Celery, Redis (task queue)
- python-jose, passlib (authentication)
- Pillow, ReportLab (image processing & PDFs)
- google-generativeai (optional Gemini integration)
- httpx, pytest (testing)

### 4.4 Configure Environment Variables

Create a `.env` file inside the `backend/` directory:

```bash
# Windows (PowerShell)
New-Item -Name ".env" -ItemType File

# Windows (CMD)
type nul > .env

# Linux / macOS
touch backend/.env
```

Paste the following into `backend/.env`:

```env
# ─── Application ───────────────────────────────────────────
APP_NAME=MORAA GemVision
APP_VERSION=1.0.0
DEBUG=true

# ─── Server ────────────────────────────────────────────────
HOST=0.0.0.0
PORT=8000

# ─── Database (SQLite for development — no external DB needed)
DATABASE_URL=postgresql://postgres.vusbefwoaliozkvrqacq:[YOUR-PASSWORD]@aws-0-ap-south-1.pooler.supabase.com:6543/postgres
# For PostgreSQL in production:
# DATABASE_URL=postgresql+psycopg2://user:password@localhost:5432/moraa_gemvision

# ─── Authentication ────────────────────────────────────────
SECRET_KEY=change-this-to-a-secure-random-string-in-production
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=7

# ─── File Uploads ──────────────────────────────────────────
MAX_UPLOAD_SIZE_MB=10
ALLOWED_EXTENSIONS=jpg,jpeg,png,webp
UPLOAD_DIR=app/uploads
REPORT_DIR=app/reports

# ─── CORS ──────────────────────────────────────────────────
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000

# ─── AI Engine ─────────────────────────────────────────────
# Options: "mock" (random) | "vision" (PIL-based real analysis)
AI_ENGINE_TYPE=vision

# ─── AI Provider (optional, for direct AI integrations) ────
PRIMARY_AI_PROVIDER=gemini
BACKUP_AI_PROVIDER=local_vision
FALLBACK_AI_PROVIDER=local_vision

# ─── Gemini API Key (optional — needed for Gemini analysis) ─
GEMINI_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

# ─── Celery / Redis ────────────────────────────────────────
# Eager mode = tasks run synchronously (no Redis needed in dev)
CELERY_TASK_ALWAYS_EAGER=true
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0

# ─── WhatsApp Onboarding (new customer registration flow) ──
# false (DEFAULT) = the existing WhatsApp image pipeline is unchanged
# true            = unregistered users are onboarded before using the service
ENABLE_ONBOARDING_GATE=False
# Gemini text model used ONLY to extract registration fields (strict JSON)
ONBOARDING_PARSER_MODEL=gemini-1.5-flash

# ─── Wallet / paid generation ───────────────────────────────
# The wallet-balance gate is always active — there is no toggle for it.
# Every inbound WhatsApp image is checked against the customer's balance.
WALLET_IMAGE_PRICE_RUPEES=500
# PSP-hosted payment page opened by the "Pay ₹500" CTA URL button.
# Leave empty to fall back to the 'recharge_500' reply button.
RECHARGE_PAYMENT_URL=
IMAGE_PREVALIDATION_ENABLED=true
IMAGE_PREVALIDATION_MODEL=gemini-2.5-flash
# true = never block a paying customer when the checker is unavailable
IMAGE_PREVALIDATION_FAIL_OPEN=true

# ─── Logging ───────────────────────────────────────────────
LOG_LEVEL=DEBUG
```

> ⚠️ **Never commit the `.env` file to version control.** It is already listed in `backend/.gitignore`.

#### What Each Variable Does

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Connection string for the database. SQLite is zero-config for dev. |
| `SECRET_KEY` | Used to sign JWT tokens. Change to a long random string in production. |
| `CORS_ORIGINS` | Comma-separated list of allowed origins (the frontend URL). |
| `AI_ENGINE_TYPE` | Which AI analysis engine to use: `mock` (random) or `vision` (real). |
| `GEMINI_API_KEY` | Google Gemini API key (only needed if using Gemini-based analysis). |
| `CELERY_TASK_ALWAYS_EAGER` | When `true`, tasks run synchronously — no Redis required. Set to `false` in production. |
| `ENABLE_ONBOARDING_GATE` | When `true`, new WhatsApp users are onboarded (welcome → registration → recharge CTA) before using the service. `false` (default) leaves the existing WhatsApp pipeline exactly as it is. |
| `ONBOARDING_PARSER_MODEL` | Gemini text model used only to extract registration fields from free-form WhatsApp messages (strict JSON). |
| `WALLET_IMAGE_PRICE_RUPEES` | Price charged per generated image, in whole Rupees. The wallet-balance gate itself is always active and has no on/off toggle. |
| `RECHARGE_PAYMENT_URL` | PSP-hosted payment page (e.g. a Razorpay link) used by the CTA URL button. Empty = fall back to the `recharge_500` reply button. |
| `IMAGE_PREVALIDATION_FAIL_OPEN` | When `true` (default), an unavailable quality checker lets the image through instead of blocking a paying customer. |

### 4.5 Run Database Migrations

```bash
alembic upgrade head
```

This creates the SQLite database file at `backend/data/moraa_gemvision.db` with all required tables,
including the additive onboarding tables (`customers`, `onboarding_sessions`) and the customer wallet
columns (`wallet_balance`, `is_registered`). The migrations only ever CREATE the two tables or ADD
the two columns — no existing table or column is renamed or dropped, existing customer rows are
back-filled by the database defaults (`wallet_balance=0`, `is_registered=true`) — and they are safe
to re-run on an existing database.

**Expected output:**
```
INFO  [alembic.runtime.migration] Running upgrade  -> <revision_id>, <description>
```

### 4.6 (Optional) Seed Data

The project does not include a seed script — the database starts empty. You can register a user via the API after starting the server.

---

## 5. Start the Backend

### 5.1 Launch the API Server

Make sure your virtual environment is still activated, then run:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Expected output:**
```
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

### 5.2 Verify the Backend is Running

Open these URLs in your browser:

| URL | Expected Result |
|---|---|
| `http://localhost:8000/` | JSON response with app name, version, and links |
| `http://localhost:8000/health` | `{"status": "healthy"}` |
| `http://localhost:8000/docs` | Interactive Swagger UI documentation |
| `http://localhost:8000/redoc` | Alternative ReDoc API documentation |

---

## 6. Frontend Setup

> **All frontend commands must be run from the `frontend/` directory.**

### 6.1 Navigate to Frontend

```bash
cd frontend
```

(If you're in the `backend/` directory, go back to the project root first: `cd ..`)

### 6.2 Install Dependencies

```bash
npm install
```

This installs all packages from `frontend/package.json`, including:
- Next.js 16, React 19, TypeScript
- Tailwind CSS v4, Framer Motion, GSAP
- Zustand (state management), React Hook Form
- Lucide React (icons), class-variance-authority
- `@google/genai` (Gemini client library)

### 6.3 Configure Frontend Environment Variables

The frontend can optionally use environment variables. Create a `.env.local` file in the `frontend/` directory:

```bash
# Windows (PowerShell)
New-Item -Name ".env.local" -ItemType File

# Windows (CMD)
type nul > .env.local

# Linux / macOS
touch frontend/.env.local
```

Paste the following:

```env
# Backend API URL (where the FastAPI server is running)
NEXT_PUBLIC_API_URL=http://localhost:8000

# Google Gemini API Key (optional — for direct Gemini analysis)
# Get your key at https://aistudio.google.com/app/apikey
GEMINI_API_KEY=
```

> ⚠️ `/api/gemini/analyze` endpoint will return an error if `GEMINI_API_KEY` is not configured. The rest of the app works without it.

#### What Each Variable Does

| Variable | Purpose |
|---|---|
| `NEXT_PUBLIC_API_URL` | Base URL of the FastAPI backend. Defaults to `http://localhost:8000`. |
| `GEMINI_API_KEY` | Google Gemini API key for direct AI analysis via the Next.js API route. |

---

## 7. Start the Frontend

```bash
npm run dev
```

**Expected output:**
```
▲ Next.js 16.2.10
- Local: http://localhost:3000
```

### 7.1 Verify the Frontend is Running

Open **http://localhost:3000** in your browser.

You should see the MORAA GemVision landing page with the application dashboard.

---

## 8. Running the Full Application

To run the **entire stack**, follow these steps in order:

### Step-by-Step

```
1. [Optional] Start Redis (if you disabled eager mode)
         ↓
2. Start the backend (FastAPI on port 8000)
         ↓
3. Wait for the health endpoint to respond
         ↓
4. Start the frontend (Next.js on port 3000)
         ↓
5. Open http://localhost:3000 in your browser
         ↓
6. Register a user and start analysing images!
```

### Two-Terminal Setup

Open **two separate terminal windows**:

| Terminal 1 — Backend (port 8000) | Terminal 2 — Frontend (port 3000) |
|---|---|
| `cd backend` | `cd frontend` |
| `venv\Scripts\activate` (Windows) | `npm run dev` |
| or `source venv/bin/activate` (Linux/Mac) | |
| `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000` | |

### Windows PowerShell Example

| Terminal 1 — Backend | Terminal 2 — Frontend |
|---|---|
| ```powershell<br>cd backend<br>.\venv\Scripts\Activate.ps1<br>uvicorn app.main:app --reload --host 0.0.0.0 --port 8000<br>``` | ```powershell<br>cd frontend<br>npm run dev<br>``` |

---

## 9. Verify the Installation

Run through this checklist to confirm everything is working:

### ✅ Backend Verification

```bash
# Health check
curl http://localhost:8000/health
# Expected: {"status": "healthy"}

# API root
curl http://localhost:8000/
# Expected: JSON with app name, version, and links

# Swagger UI (open in browser)
# http://localhost:8000/docs
```

### ✅ Frontend Verification

```bash
# Open in browser
# http://localhost:3000
```

Expected: The MORAA GemVision dashboard loads without errors.

### ✅ End-to-End Verification

1. Open **http://localhost:8000/docs** — confirm Swagger UI loads
2. Open **http://localhost:3000** — confirm the frontend loads
3. Use the Swagger UI to **POST /auth/signup** with a test user
4. Use the Swagger UI to **POST /auth/login** to get a JWT token
5. Upload an image via the frontend or **POST /upload/image**
6. Start an analysis via **POST /analysis/start**
7. View the result — the AI engine returns either mock or vision-based analysis

---

## 10. Running Celery Workers (Optional)

By default, `CELERY_TASK_ALWAYS_EAGER=true` runs analysis tasks synchronously — no Redis needed. If you want asynchronous task processing:

### 10.1 Install & Start Redis

**Windows:** Download from [redis.io/download](https://redis.io/download) or use WSL.

**Linux:**
```bash
sudo apt install redis-server
sudo service redis-server start
```

**macOS:**
```bash
brew install redis
brew services start redis
```

### 10.2 Update `.env` to Disable Eager Mode

In `backend/.env`, set:

```env
CELERY_TASK_ALWAYS_EAGER=false
```

### 10.3 Start the Celery Worker

In a **third terminal**:

```bash
cd backend
venv\Scripts\activate      # Windows
# source venv/bin/activate  # Linux/Mac

celery -A celery_worker worker -l info -Q analysis --autoreload
```

Now you have three processes running:

| Terminal | Process |
|---|---|
| Terminal 1 | FastAPI server (port 8000) |
| Terminal 2 | Next.js frontend (port 3000) |
| Terminal 3 | Celery worker (async analysis) |

---

## 11. Common Issues & Troubleshooting

### Port Already in Use

```bash
# Find what's using the port
# Windows
netstat -ano | findstr :8000

# Linux / macOS
lsof -i :8000

# Kill the process
# Replace PID with the process ID from the command above
# Windows: taskkill /PID <PID> /F
# Linux/Mac: kill -9 <PID>
```

### Python Virtual Environment Issues

```bash
# If 'python' is not found, try 'python3'
python3 -m venv venv

# If activation fails on PowerShell, run:
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### Module Not Found / pip Install Fails

```bash
# Ensure your virtual environment is activated
# Try upgrading pip first
pip install --upgrade pip

# Then retry
pip install -r requirements.txt

# If a specific package fails, install it individually
pip install <package-name>
```

### Alembic Migration Fails

```bash
# If the database file is corrupted, delete it and re-run migrations
# Windows: del data\moraa_gemvision.db
# Linux/Mac: rm data/moraa_gemvision.db

# Then re-run
alembic upgrade head
```

### CORS Issues

If the frontend can't reach the backend, ensure:

1. Backend is running on port 8000
2. `CORS_ORIGINS` in `backend/.env` includes `http://localhost:3000`
3. The frontend env var `NEXT_PUBLIC_API_URL` points to `http://localhost:8000`

### Gemini API Key Not Configured

The `/api/gemini/analyze` endpoint returns a `MISSING_API_KEY` error if:

1. `GEMINI_API_KEY` is not set in `frontend/.env.local`
2. Get a free key at https://aistudio.google.com/app/apikey

### SQLite Database Not Found

```bash
# Ensure the data directory exists
cd backend
# It will be created automatically when you run the server,
# but you can also create it manually:
mkdir data

# Then run migrations
alembic upgrade head
```

### npm Install Fails

```bash
# Clear npm cache
npm cache clean --force

# Delete node_modules and reinstall
rm -rf node_modules package-lock.json
npm install
```

### Node.js Version Mismatch

```bash
# Check your version
node --version
# Should be >= 18. If not, update Node.js from https://nodejs.org/
```

### Python Version Mismatch

```bash
# Check your version
python --version
# Should be >= 3.11
```

---

## 12. Useful Commands

### Backend

```bash
# Start the API server (with auto-reload)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000


#To run the terminaal 
npx ngrok http 8000 --url=drainpipe-unsoiled-native.ngrok-free.dev

# Run database migrations
cd backend
alembic upgrade head

# Auto-generate a new migration
alembic revision --autogenerate -m "description_of_change"

# Rollback one migration step
alembic downgrade -1

# Check current migration status
alembic current

# Run tests
cd backend
pytest
pytest -v     # verbose
pytest -k "test_name"    # run specific test

# Start Celery worker (if Redis is running)
cd backend
celery -A celery_worker worker -l info -Q analysis --autoreload
```

### Frontend

```bash
# Start development server (port 3000)
cd frontend
npm run dev

# Build for production
npm run build

# Start production server
npm run start

# Run ESLint
npm run lint
```

### Database

```bash
# SQLite is the default. The database file is at:
backend/data/moraa_gemvision.db

# To inspect the database directly:
# Windows: install DB Browser for SQLite
# Linux/Mac: sqlite3 backend/data/moraa_gemvision.db
sqlite3 backend/data/moraa_gemvision.db
.tables
```

### Docker (Coming Soon)

Docker support is planned but not yet available. See `docker/README.md` for future updates.

```bash
# When available, this will start everything:
# docker compose up --build
```

---

## 13. Project Structure

```
moraa-gemvision/
├── frontend/                      # Next.js 16 web application
│   ├── src/
│   │   ├── app/                   # App Router pages & API routes
│   │   │   ├── api/
│   │   │   │   ├── gemini/analyze/    # Gemini analysis API route
│   │   │   │   └── prompts/generate/  # Prompt generation API route
│   │   │   ├── globals.css
│   │   │   ├── layout.tsx
│   │   │   └── page.tsx
│   │   ├── components/            # Reusable UI components
│   │   │   ├── pages/             # Page-level components
│   │   │   ├── Sidebar.tsx
│   │   │   ├── Header.tsx
│   │   │   ├── UploadCard.tsx
│   │   │   └── ...more components
│   │   ├── services/              # API service layer
│   │   ├── stores/                # Zustand state management
│   │   ├── types/                 # TypeScript type definitions
│   │   ├── contexts/              # React contexts
│   │   └── lib/                   # Utilities
│   ├── next.config.ts
│   ├── package.json
│   └── tsconfig.json
│
├── backend/                       # FastAPI Python backend
│   ├── app/
│   │   ├── ai/                    # AI analysis engines
│   │   ├── api/routes/            # REST API route handlers
│   │   ├── middleware/            # CORS, logging, rate limiting
│   │   ├── models/                # SQLAlchemy ORM models
│   │   ├── schemas/               # Pydantic request/response schemas
│   │   ├── services/              # Business logic layer
│   │   ├── tasks/                 # Celery async tasks
│   │   ├── repositories/          # Data access layer
│   │   ├── utils/                 # Helpers (security, logging)
│   │   ├── config.py              # Pydantic settings
│   │   ├── database.py            # DB engine & session
│   │   ├── celery_app.py          # Celery app configuration
│   │   └── main.py                # FastAPI entry point
│   ├── alembic/                   # Database migration scripts
│   ├── providers/                 # AI provider integrations
│   ├── services/                  # Additional services
│   ├── schemas/                   # Additional schemas
│   ├── requirements.txt
│   └── README.md
│
├── ai-engine/                     # Standalone AI engine (coming soon)
├── shared/                        # Shared types (coming soon)
├── docs/                          # Documentation (coming soon)
├── docker/                        # Docker config (coming soon)
├── HOW_TO_RUN.md                  # ← You are here
├── package.json                   # Root package (minimal)
└── README.md                      # Project overview
```

---

## 14. Development Workflow

```
┌─────────────────────────────────────────────────────────┐
│                  First-Time Setup                        │
├─────────────────────────────────────────────────────────┤
│  1. Clone the repository                                │
│  2. cd frontend && npm install                          │
│  3. cd ../backend && python -m venv venv                │
│  4. Activate venv && pip install -r requirements.txt    │
│  5. Create backend/.env with your configuration         │
│  6. alembic upgrade head                                │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│                   Daily Development                      │
├─────────────────────────────────────────────────────────┤
│  Terminal 1:                           Terminal 2:      │
│  cd backend                             cd frontend      │
│  source venv/bin/activate               npm run dev      │
│  uvicorn app.main:app --reload                           │
│  ──────────────                          ──────────────  │
│  http://localhost:8000                   http://localhost:3000
│  http://localhost:8000/docs                              │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│                 Verify & Test                            │
├─────────────────────────────────────────────────────────┤
│  • Open http://localhost:3000 in browser                 │
│  • Check backend health at http://localhost:8000/health   │
│  • Explore API at http://localhost:8000/docs             │
│  • Register a user, upload an image, run an analysis     │
│  • Run tests: cd backend && pytest                       │
└─────────────────────────────────────────────────────────┘
```

---

## 15. API Routes Reference

| Method | Path | Description | Auth Required |
|---|---|---|---|
| `POST` | `/auth/signup` | Register a new user | No |
| `POST` | `/auth/login` | Login & get JWT tokens | No |
| `POST` | `/upload/image` | Upload a jewellery image | Yes |
| `POST` | `/analysis/start` | Start analysis on an image | Yes |
| `GET` | `/analysis/{id}` | Get analysis results | Yes |
| `GET` | `/history` | List analysis history | Yes |
| `GET` | `/history/{id}` | Get history detail | Yes |
| `GET` | `/reports/{id}` | Download PDF report | Yes |
| `GET` | `/health` | Health check | No |
| `GET` | `/` | API root info | No |

### Frontend-Specific API Routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/gemini/analyze` | Gemini AI analysis (Next.js API route) |
| `GET` | `/api/gemini/analyze` | Check Gemini analysis status |
| `POST` | `/api/prompts/generate` | Generate promotional prompts from analysis |

---

## 16. AI Engines

The backend supports pluggable AI analysis engines. Switch between them in `backend/.env`:

| Engine | `AI_ENGINE_TYPE` | Description |
|---|---|---|
| **Mock** | `mock` | Returns random/placeholder results — ideal for frontend development without real AI |
| **Vision** | `vision` | PIL-based real image analysis (colour detection, clarity, flaw detection) |

The frontend also supports **direct Gemini API analysis** via `/api/gemini/analyze` (requires `GEMINI_API_KEY` in `frontend/.env.local`).

---

## Quick Reference Card

| Task | Command |
|---|---|
| Backend setup | `cd backend && python -m venv venv && pip install -r requirements.txt` |
| Activate venv (Windows CMD) | `venv\Scripts\activate` |
| Activate venv (PowerShell) | `.\venv\Scripts\Activate.ps1` |
| Activate venv (Linux/Mac) | `source venv/bin/activate` |
| Run migrations | `cd backend && alembic upgrade head` |
| Start backend | `cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000` |
| Frontend setup | `cd frontend && npm install` |
| Start frontend | `cd frontend && npm run dev` |
| Run backend tests | `cd backend && pytest` |
| Run frontend lint | `cd frontend && npm run lint` |
| API docs | `http://localhost:8000/docs` |
| Health check | `http://localhost:8000/health` |
| Frontend app | `http://localhost:3000` |
