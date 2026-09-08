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

## Folder
`/Users/prashant/razorpay-headless-api`
