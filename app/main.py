import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from .executor import CodeExecutor

# 配置
API_KEY = os.getenv("API_KEY", "dify-sandbox")
MAX_REQUESTS = int(os.getenv("MAX_REQUESTS", "100"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))
WORKER_TIMEOUT = int(os.getenv("WORKER_TIMEOUT", "15"))
_max_memory_raw = os.getenv("MAX_MEMORY_MB", "").strip()
MAX_MEMORY_MB = int(_max_memory_raw) if _max_memory_raw else None

executor: CodeExecutor | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global executor
    executor = CodeExecutor(
        timeout=WORKER_TIMEOUT,
        max_memory_mb=MAX_MEMORY_MB,
    )
    try:
        yield
    finally:
        if executor is not None:
            await executor.shutdown()
            executor = None


app = FastAPI(lifespan=lifespan)


class CodeRequest(BaseModel):
    language: str
    code: str
    preload: str | None = ""
    enable_network: bool | None = False


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/v1/sandbox"):
            api_key = request.headers.get("X-Api-Key")
            if not api_key or api_key != API_KEY:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=401,
                    content={
                        "code": -401,
                        "message": "Unauthorized",
                        "data": None,
                    },
                )
        return await call_next(request)


class ConcurrencyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.semaphore = asyncio.Semaphore(MAX_WORKERS)
        self.current_requests = 0

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/v1/sandbox/run"):
            if self.current_requests >= MAX_REQUESTS:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=503,
                    content={
                        "code": -503,
                        "message": "Too many requests",
                        "data": None,
                    },
                )

            self.current_requests += 1
            try:
                async with self.semaphore:
                    response = await call_next(request)
                return response
            finally:
                self.current_requests -= 1
        return await call_next(request)


app.add_middleware(AuthMiddleware)
app.add_middleware(ConcurrencyMiddleware)


@app.get("/health")
async def health_check():
    return "ok"


@app.post("/v1/sandbox/run")
async def execute_code(request: CodeRequest):
    if request.language not in ["python3", "nodejs"]:
        return {
            "code": -400,
            "message": "unsupported language",
            "data": None,
        }

    if executor is None:
        return {
            "code": -503,
            "message": "Executor not ready",
            "data": None,
        }

    result = await executor.execute(request.code, request.language)

    return {
        "code": 0,
        "message": "success",
        "data": {
            "error": result["error"] or "",
            "stdout": result["output"] or "",
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8194)
