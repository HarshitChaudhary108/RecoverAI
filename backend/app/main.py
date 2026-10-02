from fastapi import FastAPI
from backend.app.logging_setup import setup_logging
from backend.app.webhook import router as webhook_router

app = FastAPI(title="Payment Recovery Agent API")

@app.on_event("startup")
async def startup_event():
    setup_logging()

@app.get("/health")
async def health_check():
    return {"status": "OK"}

app.include_router(webhook_router)
