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
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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

@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    logger.error(f"HTTP {exc.status_code} on {request.url.path}: {exc.detail}")
    return JSONResponse(status_code=exc.status_code, content={"status": "error", "detail": exc.detail, "code": exc.status_code})

@app.exception_handler(Exception)
async def generic_exc_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled error on {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"status": "error", "detail": "Internal server error", "code": 500})

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
    logger.info(f"Local:    http://127.0.0.1:{port}")
    logger.info(f"Network:  http://{local_ip}:{port}")
    logger.info(f"External: http://0.0.0.0:{port}")
    print(f"\n{'='*60}\n Razorpay Headless API (High-Perf, Headless, Pooled)\n  Local:    http://127.0.0.1:{port}\n  Network:  http://{local_ip}:{port}\n  External: http://0.0.0.0:{port}\n  Docs:     http://127.0.0.1:{port}/docs\n  Pool: 5 concurrent | Browser reused | Async\n{'='*60}\n")

async def generate_pay_id_async(order_id: str, key: str, amount: int, currency: str = "INR", timeout: int = 90):
    if not order_id.startswith("order_"):
        raise HTTPException(status_code=400, detail="Invalid order_id, must start with order_")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid amount")

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
            try:
                logger.info(f"[Attempt {attempt}/1] {order_id} amount={amount} timeout={timeout}s (headless=True, pooled, single)")
                ctx = await browser.new_context(viewport={"width":1280,"height":720}, user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
                page = await ctx.new_page()
                await page.set_content(html, wait_until="domcontentloaded", timeout=30000)
                try:
                    await page.wait_for_selector("iframe[src*='razorpay']", timeout=20000)
                except Exception:
                    await asyncio.sleep(3)
                await asyncio.sleep(2)

                pay_id = None
                signature = "mock"
                while time.time() - start < timeout:
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
                                            logger.warning(f"Popup err (slow net): {e}")
                            except Exception:
                                continue
                    except Exception:
                        pass
                    if pay_id:
                        break
                    await asyncio.sleep(1)

                await ctx.close()
                if pay_id:
                    elapsed_ms = int((time.time() - start)*1000)
                    logger.info(f"Success pay_id={pay_id} elapsed={elapsed_ms}ms attempt={attempt} (headless, pooled)")
                    if own_browser:
                        await browser.close()
                    return {"pay_id": pay_id, "order_id": order_id, "signature": signature, "elapsed_ms": elapsed_ms}
                else:
                    last_err = f"Timeout {timeout}s single attempt"
                    logger.warning(last_err)
                    await ctx.close()
                    raise HTTPException(status_code=500, detail=f"Failed single attempt: {last_err}")
            except HTTPException:
                if ctx:
                    try: await ctx.close()
                    except Exception: pass
                raise
            except Exception as e:
                if ctx:
                    try: await ctx.close()
                    except Exception: pass
                logger.warning(f"Single attempt failed: {e}")
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

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"\n{'='*60}\n Razorpay Headless API (High-Perf, Headless, Pooled, Async)\n  Local:    http://127.0.0.1:{port}\n  Network:  http://{_get_local_ip()}:{port}\n  External: http://0.0.0.0:{port}\n  Docs:     http://127.0.0.1:{port}/docs\n  Pool: 5 concurrent | Async | Headless=True\n{'='*60}\n")
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=True)
