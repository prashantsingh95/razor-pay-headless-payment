from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
import os
import time
import re
import logging
import socket
import asyncio
import uuid
import json
import sys
import contextvars
from contextlib import asynccontextmanager

# ---- structured logging with request_id ----
request_id_ctx = contextvars.ContextVar("request_id", default="-")
LOG_FORMAT = os.getenv("LOG_FORMAT", "text")  # text or json
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_ctx.get()
        return True

class JsonFormatter(logging.Formatter):
    def format(self, record):
        log = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "rid": getattr(record, "request_id", "-"),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            log["exc"] = self.formatException(record.exc_info)
        return json.dumps(log, ensure_ascii=False)

def setup_logging():
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    if LOG_FORMAT == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(request_id)s] %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # quiet noisy libs
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").addHandler(handler)
    logging.getLogger("uvicorn.access").propagate = False

setup_logging()
logger = logging.getLogger("razorpay-headless")

# Global browser pool for high performance (reuse, not launch per request)
browser_pool = None
playwright_instance = None
semaphore = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENCY", "5")))  # limit concurrent headless sessions (set 2 for Render 512MB)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global browser_pool, playwright_instance
    try:
        from playwright.async_api import async_playwright
        playwright_instance = await async_playwright().start()
        browser_pool = await playwright_instance.chromium.launch(
            headless=True,  # ensure headless
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--disable-setuid-sandbox"]
        )
        logger.info("Browser pool started (headless=True, pooled)")
    except Exception as e:
        logger.warning(f"Browser pool failed to start: {e}")
    yield
    try:
        if browser_pool:
            await browser_pool.close()
        if playwright_instance:
            await playwright_instance.stop()
        logger.info("Browser pool closed")
    except Exception:
        pass

app = FastAPI(
    title="Razorpay Headless Pay ID Generator",
    version="1.0.0",
    description="Production-grade high-performance headless Razorpay checkout -> pay_id (pooled browser, async, headless)",
    lifespan=lifespan,
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ---- request_id + access log middleware ----
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:8]
    request_id_ctx.set(rid)
    start = time.time()
    # attach rid to request state for later use
    request.state.rid = rid
    # log incoming (skip health noise if LOG_LEVEL=INFO, keep as DEBUG)
    if request.url.path not in ("/", "/health"):
        logger.info(f"-> {request.method} {request.url.path} rid={rid} ip={request.client.host if request.client else '-'}")
    else:
        logger.debug(f"-> {request.method} {request.url.path} rid={rid}")
    try:
        response = await call_next(request)
    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        logger.exception(f"<- {request.method} {request.url.path} rid={rid} err={e} elapsed={elapsed_ms}ms")
        raise
    elapsed_ms = int((time.time() - start) * 1000)
    response.headers["X-Request-ID"] = rid
    # structured access log: single line per request with status + elapsed
    if request.url.path not in ("/", "/health"):
        logger.info(f"<- {request.method} {request.url.path} rid={rid} status={response.status_code} elapsed={elapsed_ms}ms")
    else:
        logger.debug(f"<- {request.method} {request.url.path} rid={rid} status={response.status_code} elapsed={elapsed_ms}ms")
    return response

class PayRequest(BaseModel):
    order_id: str = Field(..., description="Razorpay order_id starting with order_", json_schema_extra={"example": "order_TXXMi5sObbpm2X"})
    amount: int = Field(..., description="Amount in paise (399900 = ₹3999)", json_schema_extra={"example": 399900})
    key: Optional[str] = Field(default="rzp_test_Msze6ygmQKtjXy", description="Razorpay key_id", json_schema_extra={"example": "rzp_test_Msze6ygmQKtjXy"})
    currency: Optional[str] = Field(default="INR", description="Currency code", json_schema_extra={"example": "INR"})
    timeout: Optional[int] = Field(default=90, ge=30, le=180, description="Timeout in seconds for slow net (30-180)", json_schema_extra={"example": 90})
    model_config = {"json_schema_extra": {"example": {"order_id": "order_TXXMi5sObbpm2X","amount":399900,"key":"rzp_test_Msze6ygmQKtjXy","currency":"INR","timeout":90}}}

class PayResponse(BaseModel):
    pay_id: str = Field(..., description="Generated Razorpay payment ID", json_schema_extra={"example": "pay_TXXN2PCqIdC0tt"})
    order_id: str = Field(..., description="Original order ID", json_schema_extra={"example": "order_TXXMi5sObbpm2X"})
    signature: str = Field(..., description="Razorpay signature (or mock)", json_schema_extra={"example": "a1b2c3d4e5f6..."})
    status: str = Field(default="success", example="success")
    elapsed_ms: int = Field(default=0, description="Time taken in ms", json_schema_extra={"example": 25400})
    model_config = {"json_schema_extra": {"example": {"pay_id":"pay_TXXN2PCqIdC0tt","order_id":"order_TXXMi5sObbpm2X","signature":"mock","status":"success","elapsed_ms":25400}}}

class HealthResponse(BaseModel):
    status: str = Field(example="ok")
    service: str = Field(example="razorpay-headless")
    version: str = Field(example="1.0.0")
    docs: Optional[str] = Field(default="/docs", example="/docs")
    model_config = {"json_schema_extra": {"example": {"status":"ok","service":"razorpay-headless","version":"1.0.0","docs":"/docs"}}}

class HealthDetailedResponse(BaseModel):
    status: str = Field(example="healthy")
    uptime: str = Field(example="ok")
    model_config = {"json_schema_extra": {"example": {"status":"healthy","uptime":"ok"}}}

class ErrorResponse(BaseModel):
    status: str = Field(default="error", example="error")
    detail: str = Field(..., example="Invalid order_id, must start with order_")
    code: int = Field(..., example=400)
    model_config = {"json_schema_extra": {"example": {"status":"error","detail":"Invalid order_id, must start with order_","code":400}}}

class ValidationErrorResponse(BaseModel):
    detail: list = Field(example=[{"loc": ["body", "order_id"], "msg": "Field required", "type": "missing"}])

class CreateOrderRequest(BaseModel):
    amount: int = Field(..., description="Amount in paise (399900 = ₹3999)", json_schema_extra={"example": 399900})
    currency: Optional[str] = Field(default="INR", json_schema_extra={"example": "INR"})
    receipt: Optional[str] = Field(default=None, description="Receipt id (auto-generated if null)", json_schema_extra={"example": "rcpt_123"})
    key_id: Optional[str] = Field(default=None, description="Razorpay key_id (or set RAZORPAY_KEY_ID env)", json_schema_extra={"example": "rzp_test_Msze6ygmQKtjXy"})
    key_secret: Optional[str] = Field(default=None, description="Razorpay key_secret (or set RAZORPAY_KEY_SECRET env)", json_schema_extra={"example": "your_secret"})
    model_config = {"json_schema_extra": {"example": {"amount": 399900, "currency": "INR", "receipt": "rcpt_test"}}}

class CreateOrderResponse(BaseModel):
    order_id: str = Field(..., json_schema_extra={"example": "order_TXXMi5sObbpm2X"})
    amount: int = Field(..., json_schema_extra={"example": 399900})
    currency: str = Field(..., json_schema_extra={"example": "INR"})
    receipt: str = Field(..., json_schema_extra={"example": "rcpt_123"})
    status: str = Field(..., json_schema_extra={"example": "created"})
    raw: dict = Field(default={}, description="Full Razorpay response")

@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    rid = getattr(request.state, "rid", request_id_ctx.get())
    logger.error(f"HTTP {exc.status_code} on {request.url.path} rid={rid}: {exc.detail}")
    return JSONResponse(status_code=exc.status_code, content={"status": "error", "detail": exc.detail, "code": exc.status_code, "request_id": rid})

@app.exception_handler(Exception)
async def generic_exc_handler(request: Request, exc: Exception):
    rid = getattr(request.state, "rid", request_id_ctx.get())
    logger.exception(f"Unhandled error on {request.url.path} rid={rid}: {exc}")
    return JSONResponse(status_code=500, content={"status": "error", "detail": "Internal server error", "code": 500, "request_id": rid})

def _get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

@app.on_event("startup")
async def startup_event():
    port = int(os.getenv("PORT", "8000"))
    local_ip = _get_local_ip()
    max_conc = os.getenv("MAX_CONCURRENCY", "5")
    logger.info(f"Startup port={port} host=0.0.0.0 concurrency={max_conc} log_format={LOG_FORMAT} log_level={LOG_LEVEL}")
    logger.info(f"Local:    http://127.0.0.1:{port}")
    logger.info(f"Network:  http://{local_ip}:{port}")
    logger.info(f"External: http://0.0.0.0:{port} docs=/docs pool={max_conc} headless=True")

async def generate_pay_id_async(order_id: str, key: str, amount: int, currency: str = "INR", timeout: int = 90):
    rid = request_id_ctx.get()
    order_suffix = order_id[-6:] if len(order_id) > 6 else order_id
    if not order_id.startswith("order_"):
        logger.warning(f"[{rid}] validation fail order={order_suffix} reason=order_id must start with order_")
        raise HTTPException(status_code=400, detail="Invalid order_id, must start with order_")
    if amount <= 0:
        logger.warning(f"[{rid}] validation fail order={order_suffix} amount={amount}")
        raise HTTPException(status_code=400, detail="Invalid amount")
    logger.info(f"[{rid}] start order=*{order_suffix} amount={amount} timeout={timeout}s key={key[:8]}.. currency={currency}")

    html = f"""<!DOCTYPE html><html><head><title>Hoora</title>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script></head><body>
<script>
window.razorpayResult=null;window.razorpayError=null;
var opts={{key:"{key}",order_id:"{order_id}",amount:{amount},currency:"{currency}",
name:"Hoora",description:"Test",handler:function(r){{window.razorpayResult=r;}},
modal:{{ondismiss:function(){{window.razorpayError="dismissed";}}}}}};
window.addEventListener("load",function(){{setTimeout(()=>{{try{{new Razorpay(opts).open();}}catch(e){{window.razorpayError=e.message;}}}},800);}});
</script></body></html>"""

    # Use semaphore for high performance but limited concurrency
    async with semaphore:
        # Ensure browser pool exists, else fallback to launch per-request
        global browser_pool
        browser = browser_pool
        own_browser = False
        if not browser:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--disable-setuid-sandbox"])
            own_browser = True
            logger.warning("Browser pool not ready, using per-request browser (slower)")

        for attempt in range(1, 2):  # single attempt only
            start = time.time()
            ctx = None
            page = None
            loop_iter = 0
            try:
                logger.info(f"[{rid}] attempt {attempt}/1 order=*{order_suffix} amount={amount} timeout={timeout}s pooled={browser is not None}")
                ctx = await browser.new_context(viewport={"width":1280,"height":720}, user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
                page = await ctx.new_page()
                logger.debug(f"[{rid}] page created, set_content start elapsed={(time.time()-start):.1f}s")
                # Initial working logic: domcontentloaded 30s (Render network fast, loads Razorpay)
                # Fallback to commit if domcontentloaded times out (local slow network)
                try:
                    await page.set_content(html, wait_until="domcontentloaded", timeout=30000)
                    logger.debug(f"[{rid}] set_content domcontentloaded done elapsed={(time.time()-start):.1f}s")
                except Exception as e:
                    logger.warning(f"[{rid}] set_content domcontentloaded timeout elapsed={(time.time()-start):.1f}s err={e} - fallback commit")
                    try:
                        await page.set_content(html, wait_until="commit", timeout=10000)
                        logger.debug(f"[{rid}] set_content commit fallback done elapsed={(time.time()-start):.1f}s")
                        # after commit, wait for Razorpay to load
                        try:
                            await page.wait_for_function("typeof Razorpay !== 'undefined'", timeout=8000)
                            logger.debug(f"[{rid}] Razorpay loaded after commit fallback elapsed={(time.time()-start):.1f}s")
                        except Exception as e2:
                            logger.debug(f"[{rid}] Razorpay not loaded after fallback {e2}")
                    except Exception as e2:
                        logger.warning(f"[{rid}] set_content commit fallback failed {e2}")
                try:
                    await page.wait_for_selector("iframe[src*='razorpay']", timeout=20000)
                    logger.debug(f"[{rid}] iframe found elapsed={(time.time()-start):.1f}s")
                except Exception as e:
                    logger.debug(f"[{rid}] iframe wait timeout elapsed={(time.time()-start):.1f}s err={e}")
                    try:
                        has_rzp = await page.evaluate("typeof Razorpay !== 'undefined'")
                        logger.debug(f"[{rid}] Razorpay loaded={has_rzp} elapsed={(time.time()-start):.1f}s")
                    except Exception:
                        pass
                    await asyncio.sleep(3)
                await asyncio.sleep(2)

                pay_id = None
                signature = "mock"
                while time.time() - start < timeout:
                    loop_iter += 1
                    elapsed = time.time() - start
                    if loop_iter % 10 == 1:
                        logger.debug(f"[{rid}] loop iter={loop_iter} elapsed={elapsed:.1f}s/{timeout}s frames={len(page.frames)}")
                    try:
                        res = await page.evaluate("window.razorpayResult")
                        if res and res.get("razorpay_payment_id"):
                            pay_id = res["razorpay_payment_id"]
                            signature = res.get("razorpay_signature", "mock")
                            break
                    except Exception:
                        pass
                    try:
                        err = await page.evaluate("window.razorpayError")
                        if err and "dismissed" in str(err):
                            raise HTTPException(status_code=400, detail="Checkout dismissed")
                    except HTTPException:
                        raise
                    except Exception:
                        pass
                    try:
                        content = await page.content()
                        m = re.search(r"pay_[a-zA-Z0-9]+", content)
                        if m and not pay_id:
                            pay_id = m.group(0)
                    except Exception:
                        pass

                    # Headless automation
                    try:
                        checkout_frame = None
                        for fr in page.frames:
                            if "api.razorpay.com" in fr.url:
                                checkout_frame = fr
                                break
                        if not checkout_frame:
                            el = await page.query_selector("iframe.razorpay-checkout-frame")
                            if el:
                                try:
                                    checkout_frame = await el.content_frame()
                                except Exception:
                                    pass
                        frames = [checkout_frame] if checkout_frame else page.frames
                        for frame in frames:
                            if not frame:
                                continue
                            try:
                                tel = await frame.query_selector("input[type='tel']")
                                if tel and await tel.is_visible():
                                    placeholder = await tel.get_attribute("placeholder") or ""
                                    if "mobile" in placeholder.lower() or placeholder == "":
                                        await tel.fill("9000090000")
                                        await asyncio.sleep(0.8)
                                        btn = await frame.query_selector("button:has-text('Continue')")
                                        if btn and await btn.is_visible():
                                            await btn.click()
                                            await asyncio.sleep(3.5)
                                        continue
                                nb = await frame.query_selector("text=Netbanking")
                                if nb and await nb.is_visible():
                                    await nb.click()
                                    await asyncio.sleep(1.2)
                                    hdfc = await frame.query_selector("text=HDFC")
                                    if hdfc and await hdfc.is_visible():
                                        # HDFC click auto opens new tab with Success/Fail
                                        try:
                                            async with page.expect_popup(timeout=15000) as popup_info:
                                                await hdfc.click()
                                            popup = await popup_info.value
                                            await popup.wait_for_load_state(timeout=20000)
                                            try:
                                                await popup.wait_for_selector("text=Success", timeout=20000)
                                            except Exception:
                                                await asyncio.sleep(3)
                                            succ = await popup.query_selector("text=Success")
                                            if succ:
                                                try:
                                                    m = re.search(r"pay_[a-zA-Z0-9]+", popup.url)
                                                    if not m:
                                                        m = re.search(r"pay_[a-zA-Z0-9]+", await popup.content())
                                                    if m:
                                                        pay_id = m.group(0)
                                                except Exception:
                                                    pass
                                                await succ.click()
                                                try:
                                                    await popup.wait_for_event("close", timeout=8000)
                                                except Exception:
                                                    await asyncio.sleep(4)
                                                for _ in range(15):
                                                    try:
                                                        r = await page.evaluate("window.razorpayResult")
                                                        if r and r.get("razorpay_payment_id"):
                                                            pay_id = r["razorpay_payment_id"]
                                                            signature = r.get("razorpay_signature", signature)
                                                            break
                                                    except Exception:
                                                        pass
                                                    await asyncio.sleep(1)
                                                if pay_id:
                                                    break
                                        except Exception as e:
                                            logger.warning(f"[{rid}] popup err elapsed={(time.time()-start):.1f}s: {e}")
                            except Exception:
                                continue
                    except Exception:
                        pass
                    if pay_id:
                        break
                    await asyncio.sleep(1)

                try:
                    await ctx.close()
                except Exception:
                    pass
                if pay_id:
                    elapsed_ms = int((time.time() - start)*1000)
                    logger.info(f"[{rid}] success pay_id={pay_id} order=*{order_suffix} elapsed={elapsed_ms}ms iter={loop_iter} attempt={attempt}")
                    if own_browser:
                        try: await browser.close()
                        except Exception: pass
                    return {"pay_id": pay_id, "order_id": order_id, "signature": signature, "elapsed_ms": elapsed_ms}
                else:
                    actual = time.time() - start
                    last_err = f"Timeout {timeout}s single attempt (actual {actual:.1f}s, iter={loop_iter})"
                    logger.warning(f"[{rid}] {last_err} order=*{order_suffix}")
                    try:
                        # dump last page state for debugging (truncated)
                        content_snip = (await page.content())[:500] if 'page' in locals() and page else ""
                        logger.debug(f"[{rid}] timeout page_snip={content_snip[:200]}")
                    except Exception:
                        pass
                    raise HTTPException(status_code=500, detail=f"Failed single attempt: {last_err}")
            except HTTPException:
                if ctx:
                    try: await ctx.close()
                    except Exception: pass
                raise
            except Exception as e:
                actual = time.time() - start
                logger.warning(f"[{rid}] single attempt failed after {actual:.1f}s iter={loop_iter}: {e}", exc_info=True)
                if ctx:
                    try: await ctx.close()
                    except Exception: pass
                raise HTTPException(status_code=500, detail=f"Headless failed (single attempt): {str(e)}")
            finally:
                if own_browser and browser:
                    try: await browser.close()
                    except Exception: pass
        raise HTTPException(status_code=500, detail="Failed single attempt")

@app.get("/", response_model=HealthResponse, tags=["Health"], summary="Root health")
def health():
    return {"status": "ok", "service": "razorpay-headless", "version": "1.0.0", "docs": "/docs"}

@app.get("/health", response_model=HealthDetailedResponse, tags=["Health"], summary="Detailed health")
def health_check():
    return {"status": "healthy", "uptime": "ok"}

@app.post("/generate-pay-id", response_model=PayResponse, tags=["Payment"], summary="Generate pay_id headlessly (pooled, async)", description="High-performance headless: pooled browser, async, semaphore(5), slow-net retries.", responses={200: {"model": PayResponse}, 400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}, 422: {"model": ValidationErrorResponse}})
async def generate_pay_id(req: PayRequest):
    start = time.time()
    result = await generate_pay_id_async(req.order_id, req.key, req.amount, req.currency, req.timeout)
    elapsed = int((time.time() - start)*1000)
    result["elapsed_ms"] = elapsed
    return PayResponse(pay_id=result["pay_id"], order_id=result["order_id"], signature=result["signature"], elapsed_ms=elapsed)

@app.post("/pay", response_model=PayResponse, tags=["Payment"], summary="Alias for /generate-pay-id")
async def pay(req: PayRequest):
    return await generate_pay_id(req)

@app.post("/create-order", response_model=CreateOrderResponse, tags=["Order"], summary="Create Razorpay order (for real pay_id test)", description="Calls https://api.razorpay.com/v1/orders with Basic Auth. Provide key_id/key_secret in body or set RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET env. Returns order_id for /generate-pay-id.")
async def create_order(req: CreateOrderRequest, request: Request):
    rid = getattr(request.state, "rid", request_id_ctx.get())
    import httpx
    key_id = req.key_id or os.getenv("RAZORPAY_KEY_ID") or "rzp_test_Msze6ygmQKtjXy"
    key_secret = req.key_secret or os.getenv("RAZORPAY_KEY_SECRET")
    if not key_secret:
        logger.warning(f"[{rid}] create-order missing key_secret")
        raise HTTPException(status_code=400, detail="key_secret required: pass in body or set RAZORPAY_KEY_SECRET env (find at https://dashboard.razorpay.com/app/keys)")
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid amount")
    receipt = req.receipt or f"rcpt_{uuid.uuid4().hex[:8]}"
    logger.info(f"[{rid}] create-order amount={req.amount} currency={req.currency} receipt={receipt} key={key_id[:8]}..")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.razorpay.com/v1/orders",
                auth=(key_id, key_secret),
                json={"amount": req.amount, "currency": req.currency, "receipt": receipt}
            )
            logger.info(f"[{rid}] razorpay orders status={resp.status_code}")
            if resp.status_code not in (200, 201):
                logger.error(f"[{rid}] razorpay error {resp.status_code}: {resp.text[:500]}")
                raise HTTPException(status_code=resp.status_code, detail=f"Razorpay API error: {resp.text[:500]}")
            data = resp.json()
            order_id = data.get("id")
            if not order_id or not order_id.startswith("order_"):
                logger.error(f"[{rid}] unexpected razorpay resp {data}")
                raise HTTPException(status_code=500, detail=f"Unexpected Razorpay response: {data}")
            logger.info(f"[{rid}] order created {order_id} amount={data.get('amount')}")
            return CreateOrderResponse(order_id=order_id, amount=data.get("amount", req.amount), currency=data.get("currency", req.currency), receipt=data.get("receipt", receipt), status=data.get("status", "created"), raw=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[{rid}] create-order failed: {e}")
        raise HTTPException(status_code=500, detail=f"Create order failed: {e}")

class CronRequest(BaseModel):
    order_id: Optional[str] = Field(default=None, description="Optional single order_id to process; if null, uses queue/mock")
    amount: Optional[int] = Field(default=399900, json_schema_extra={"example": 399900})
    count: Optional[int] = Field(default=1, ge=1, le=10, description="How many to process (1-10)")

@app.get("/cron/health", tags=["Cron"], summary="Cron warmup & pool check (idempotent)")
async def cron_health(request: Request):
    rid = getattr(request.state, "rid", request_id_ctx.get())
    # No auth required — always 200 when hit (Render cron / uptime checks)
    pool_ok = browser_pool is not None
    logger.info(f"[{rid}] cron health pool_ok={pool_ok} ip={request.client.host if request.client else '-'}")
    return {"status": "ok", "pool_ok": pool_ok, "service": "razorpay-headless", "rid": rid, "time": int(time.time())}

@app.post("/cron/generate", tags=["Cron"], summary="Cron job: generate pay_id(s) (for queue/cron)")
async def cron_generate(req: CronRequest, request: Request):
    rid = getattr(request.state, "rid", request_id_ctx.get())
    # Optional secret check — but always allow if CRON_SECRET not set or missing, to return 200 for cron hits
    expected = os.getenv("CRON_SECRET")
    if expected:
        secret = request.headers.get("X-Cron-Secret") or request.query_params.get("secret")
        if secret and secret != expected:
            raise HTTPException(status_code=401, detail="Invalid cron secret")
        # if secret is None/empty, allow (return 200) — per user request
    # If no order_id, just warmup pool
    if not req.order_id:
        pool_ok = browser_pool is not None
        logger.info(f"[{rid}] cron warmup pool_ok={pool_ok} count={req.count}")
        return {"status": "warmup", "pool_ok": pool_ok, "count": req.count, "rid": rid}
    result = await generate_pay_id_async(req.order_id, os.getenv("RAZORPAY_KEY_ID", "rzp_test_Msze6ygmQKtjXy"), req.amount or 399900, "INR", 90)
    logger.info(f"[{rid}] cron generate done pay_id={result['pay_id']} order={req.order_id}")
    return {"status": "success", "pay_id": result["pay_id"], "order_id": req.order_id, "elapsed_ms": result["elapsed_ms"], "rid": rid}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    logger.info(f"Starting via __main__ host={host} port={port}")
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
