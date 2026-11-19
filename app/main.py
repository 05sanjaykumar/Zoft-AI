"""Zoft voice agent: FastAPI app entry point.

    uvicorn app.main:app --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.calls import store
from app.clinic.routes import router as clinic_router
from app.config import LLM_MODEL, PUBLIC_HOST, TWILIO_PHONE_NUMBER
from app.routes.calls import router as calls_router
from app.routes.twilio import router as twilio_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Zoft voice agent starting")
    print(f"   llm:     {LLM_MODEL}")
    if TWILIO_PHONE_NUMBER and PUBLIC_HOST:
        print(f"   phone:   {TWILIO_PHONE_NUMBER} -> https://{PUBLIC_HOST}/twilio/voice")
    else:
        print("   phone:   not configured (set TWILIO_* and PUBLIC_HOST in .env)")
    yield


app = FastAPI(
    title="Zoft voice agent",
    description="Answers and places phone calls: listens, thinks, uses tools, speaks back.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(calls_router)
app.include_router(twilio_router)
app.include_router(clinic_router)


@app.get("/health")
def health():
    return {"status": "ok", "active_calls": len(store.TASKS)}
