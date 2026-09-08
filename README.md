# Razorpay Headless Pay ID API

FastAPI service that generates `pay_xxx` via headless Razorpay checkout (no UI).

**Flow:** `order_id` + `amount` -> headless Chromium -> fill mobile `9000090000` -> Netbanking -> HDFC -> Success -> `pay_id`

## Install
```bash
pip install -r requirements.txt
playwright install chromium
```

## Run
```bash
uvicorn app:app --reload --port 8000
# or
python app.py
```

## Endpoints

### POST /generate-pay-id
**Request:**
```json
{
  "order_id": "order_XXXX",
  "amount": 399900,
  "key": "rzp_test_Msze6ygmQKtjXy",
  "currency": "INR"
}
```

**Response:**
```json
{
  "pay_id": "pay_XXXX",
  "order_id": "order_XXXX",
  "signature": "mock_or_real",
  "status": "success"
}
```

**Alias:** `POST /pay` same as above

### GET / , GET /health
Health check

## Test
```bash
curl -X POST http://localhost:8000/generate-pay-id \
  -H "Content-Type: application/json" \
  -d '{"order_id":"order_TXW...","amount":399900}'
```

## Deploy to Render

**Will it work as-is?** Almost — but 2 fixes required for Render:

1. **Build command** must install Playwright system deps (not just `playwright install chromium`):
   ```
   pip install -r requirements.txt && python -m playwright install --with-deps chromium
   ```
   Local `run.sh:51` only does `playwright install chromium` which misses Linux deps → `Host system is missing dependencies` on Render Ubuntu.

2. **Start command** must NOT use `--reload` and must bind `$PORT`:
   ```
   uvicorn app:app --host 0.0.0.0 --port $PORT
   ```
   `run.sh:84` uses `--reload` (dev only, double memory, watcher). Render already injects `$PORT` (10000), `app.py:325` handles it correctly.

**Already fixed in this repo:**
- `render.yaml:5-6` — correct build+start commands, `healthCheckPath: /`
- `Dockerfile:1-14` — alternative Docker deploy (recommended for Playwright stability)
- `app.py:20` — `MAX_CONCURRENCY` env (set `2` on free 512MB plan, `5` needs 1GB+ Standard)
- `app.py:28-30` & `app.py:145` — `--no-sandbox --disable-dev-shm-usage` for Render containers

**Render Dashboard settings (if not using `render.yaml`):**
- Runtime: `Python 3.11` or `Docker`
- Build Command: `pip install --upgrade pip && pip install -r requirements.txt && python -m playwright install --with-deps chromium`
- Start Command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Health Check: `/` or `/health`
- Plan: **Standard 1GB+** (Chromium ~400MB + pooled pages; free 512MB will OOM with `semaphore=5`). Or set env `MAX_CONCURRENCY=2`.

**Timeout note:** `app.py:59` default `timeout=90s`. Render proxy times out at 100s — keep ≤90s.

## Folder
`/Users/prashant/razorpay-headless-api`
