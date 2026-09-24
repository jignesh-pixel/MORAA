# 💎 MORAA GemVision

**AI-Powered Jewellery Analysis Platform**

MORAA GemVision is a full-stack application that uses AI to analyze jewellery images. Upload a photo of a gemstone or jewellery piece, and the platform evaluates quality, identifies characteristics, detects potential flaws, and generates professional PDF reports.

---

## 📦 Project Structure

```
moraa-gemvision/
├── frontend/          # Next.js 16 + React 19 web application
├── backend/           # FastAPI REST API with Celery task queue
├── ai-engine/         # AI analysis engine (coming soon)
├── shared/            # Shared type definitions (coming soon)
├── docs/              # Project documentation (coming soon)
└── docker/            # Docker / Compose configuration (coming soon)
```

---

## 🚀 Quick Start

### Prerequisites

- **Node.js** >= 18 (recommended: 20 LTS)
- **Python** >= 3.11
- **Redis** (optional — only needed for async Celery task processing)
- **PostgreSQL** (optional — SQLite works out of the box for development)

---

### 1️⃣ Frontend (Next.js)

```bash
cd frontend
npm install
npm run dev
```

The frontend starts at **http://localhost:3000**.

| Command | Description |
|---|---|
| `npm run dev` | Start development server with hot reload |
| `npm run build` | Build for production |
| `npm run start` | Start production server |
| `npm run lint` | Run ESLint |

---

### 2️⃣ Backend (FastAPI)

```bash
cd backend

# Create and activate a virtual environment (recommended)
python -m venv venv
venv\Scripts\activate       # Windows
#source venv/bin/activate      # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Run database migrations
alembic upgrade head

# Start the API server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API server starts at **http://localhost:8000**.

| Endpoint | Description |
|---|---|
| `http://localhost:8000/docs` | Swagger UI (interactive API docs) |
| `http://localhost:8000/redoc` | ReDoc (alternative API docs) |
| `http://localhost:8000/health` | Health check endpoint |

#### 📬 Running Celery Workers (optional)

If you have Redis running and want async task processing:

```bash
# Terminal 1: Start the Celery worker
cd backend
celery -A celery_worker worker -l info -Q analysis --autoreload

# Terminal 2: Start the API server
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

> **For development:** By default, `CELERY_TASK_ALWAYS_EAGER=True` so tasks run synchronously without needing Redis. Set it to `False` in production.

---

### 3️⃣ Run Both Together (Full Stack)

This is how you run the **entire application** — frontend and backend — side by side.

#### 📋 First-Time Setup

Do these steps **once** when you first clone the project:

```bash
# --- Frontend Setup ---
cd frontend
npm install

# --- Backend Setup ---
cd ../backend
python -m venv venv
source venv/bin/activate      # Linux/Mac
# venv\Scripts\activate       # Windows
pip install -r requirements.txt
alembic upgrade head
```

#### 🖥️ Running Both Servers

Open **two separate terminal windows**:

| Terminal 1 — Frontend | Terminal 2 — Backend |
|---|---|
| ![Terminal 1 icon](https://img.icons8.com/fluency/28/null/console.png) | ![Terminal 2 icon](https://img.icons8.com/fluency/28/null/console.png) |
| ```bash<br>cd frontend<br>npm run dev<br>``` | ```bash<br>cd backend<br>source venv/bin/activate<br>uvicorn app.main:app --reload --host 0.0.0.0 --port 8000<br>``` |
| Starts on **http://localhost:3000** | Starts on **http://localhost:8000** |

**On Windows (PowerShell):**

| Terminal 1 — Frontend | Terminal 2 — Backend |
|---|---|
| ```powershell<br>cd frontend<br>npm run dev<br>``` | ```powershell<br>cd backend<br>.\venv\Scripts\activate<br>uvicorn app.main:app --reload --host 0.0.0.0 --port 8000<br>``` |

#### ✅ Verify It's Working

1. Open **http://localhost:3000** — you should see the MORAA GemVision frontend
2. Open **http://localhost:8000/docs** — you should see the Swagger API documentation
3. Open **http://localhost:8000/health** — you should see a JSON health-check response

---

### 4️⃣ First Run Checklist

- [ ] Frontend dependencies installed (`cd frontend && npm install`)
- [ ] Backend virtual environment created (`cd backend && python -m venv venv`)
- [ ] Backend dependencies installed (`pip install -r requirements.txt`)
- [ ] Backend `.env` file configured (see example below)
- [ ] Database migrations applied (`alembic upgrade head`)
- [ ] Frontend running on port **3000**
- [ ] Backend running on port **8000**
- [ ] CORS configured to allow `http://localhost:3000`

---

## 🧪 Example `.env` (Backend)

Copy this into `backend/.env`:

```env
# Application
APP_NAME=MORAA GemVision
APP_VERSION=1.0.0
DEBUG=true

# Server
HOST=0.0.0.0
PORT=8000

# Database (SQLite is default — switch to PostgreSQL for production)
DATABASE_URL=sqlite:///./data/moraa_gemvision.db
# DATABASE_URL=postgresql+psycopg2://user:password@localhost:5432/moraa_gemvision

# Auth
SECRET_KEY=your-super-secret-key-change-in-production
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=7

# Uploads
UPLOAD_DIR=app/uploads
REPORT_DIR=app/reports
MAX_UPLOAD_SIZE_MB=10
ALLOWED_EXTENSIONS=jpg,jpeg,png,webp

# CORS
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000

# AI Engine (mock | vision)
AI_ENGINE_TYPE=vision

# Celery / Redis (not needed in dev — eager mode is on by default)
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0
CELERY_TASK_ALWAYS_EAGER=true

# Logging
LOG_LEVEL=DEBUG
```

---

## 🧠 AI Engine

The backend supports pluggable AI analysis engines:

- **`mock`** — Returns random/placeholder analysis results (great for frontend development without real AI)
- **`vision`** — Uses PIL-based image analysis to detect colour, clarity, cut, and potential flaws

Switch between them by setting `AI_ENGINE_TYPE=mock` or `AI_ENGINE_TYPE=vision` in your `.env`.

This is separate from **image generation** (turning a jewellery photo into a
polished product shot), which is its own provider chain — see below.

---

## 🖼️ Image Generation

`POST /api/generate-image` generates a product image from a text prompt and
an optional reference image, via `ImageGenerationManager`
(`backend/app/ai/image_generation_manager.py`):

- **Primary provider:** OpenAI (`gpt-image-1`, reference-aware editing)
- **Fallback provider:** Gemini — used automatically on recoverable errors
  (HTTP 429, timeouts, 5xx); quota/billing exhaustion and non-recoverable
  errors (invalid prompt, safety block) halt immediately with no fallback
  and no automatic retries.
- Style-specific prompt builders live in `backend/app/services/*_prompt.py`
  (e.g. e-commerce main image, macro shot, scale reference) and share a
  common product-fidelity instruction block
  (`backend/app/ai/product_fidelity.py`) that tells the model to preserve
  the uploaded product's geometry, stones, and metal colour exactly.

### WhatsApp catalog pack (paid)

Customers can also send a photo directly over WhatsApp
(`POST /api/meta/webhook`). Each image is billed one flat price
(`WALLET_IMAGE_PRICE_RUPEES`, default ₹500) against the customer's prepaid
wallet balance before a 7-style catalog pack is generated and delivered
back over WhatsApp — see `backend/app/services/wallet_service.py` and
`backend/app/services/meta_whatsapp_service.py`. The wallet-balance check
is always active; there is no configuration flag to disable it.

---

## 🛠️ Tech Stack

### Frontend
| Technology | Purpose |
|---|---|
| Next.js 16 | React framework with App Router |
| React 19 | UI library |
| TypeScript | Type safety |
| Tailwind CSS v4 | Utility-first CSS |
| Framer Motion | Animations & transitions |
| Zustand | State management |
| React Hook Form | Form handling |
| Lucide React | Icon library |

### Backend
| Technology | Purpose |
|---|---|
| FastAPI | REST API framework (Python) |
| SQLAlchemy 2.0 | ORM / database access |
| Alembic | Database migrations |
| Celery | Async task queue |
| Redis | Celery broker & result backend |
| JWT (python-jose) | Authentication |
| ReportLab | PDF report generation |
| Pillow | Image processing |
| Pydantic | Data validation & settings |

---

## 🗺️ Database Migrations

```bash
cd backend

# Create a new migration
alembic revision --autogenerate -m "description_of_change"

# Apply pending migrations
alembic upgrade head

# Rollback one step
alembic downgrade -1
```

---

## 📚 API Routes

| Method | Path | Description |
|---|---|---|
| POST | `/auth/signup` | Register a new user |
| POST | `/auth/login` | Login & get JWT tokens |
| POST | `/upload/image` | Upload a jewellery image |
| POST | `/analysis/start` | Start analysis on an uploaded image |
| GET | `/analysis/{id}` | Get analysis results |
| GET | `/history` | List analysis history |
| GET | `/history/{id}` | Get history detail |
| GET | `/reports/{id}` | Download PDF report |
| POST | `/api/generate-image` | Generate a product image (OpenAI primary, Gemini fallback) |
| POST | `/api/meta/webhook` | WhatsApp Cloud API webhook (paid catalog-pack generation) |
| GET | `/api/meta/webhook/failure-rate` | Windowed generation failure rate (auth required) |
| POST | `/api/payments/razorpay/webhook` | Razorpay payment webhook (wallet top-up) |
| GET | `/health` | Health check |

---

## 📄 License

Proprietary — MORAA GemVision
