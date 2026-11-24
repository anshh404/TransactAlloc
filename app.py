"""ASGI application object.

Split out of main.py so `uvicorn app:app` and `python main.py serve` both
work, and so tests can import the app without launching a server.
"""

from contextlib import asynccontextmanager
from pathlib import Path
import sys

# See main.py: uvicorn may run on a cp1252 console where printing an arrow
# raises UnicodeEncodeError inside a worker thread.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

import api
import auth


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Idempotent: older databases gain password verifiers on restart.
    try:
        auth.ensure_passwords()
    except Exception:
        # A missing database must not stop the app from starting; the login
        # endpoint will report the connection problem to the client.
        pass
    yield


app = FastAPI(title="TransactAlloc", version="1.0.0",
              description="Adaptive cloud resource manager with fragmentation-aware reallocation",
              lifespan=lifespan)
app.include_router(api.router, prefix="/api")


@app.get("/healthz")
@app.get("/health")
def healthz():
    return JSONResponse({"ok": True})


_static = Path(__file__).parent / "static"
if _static.is_dir():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")
