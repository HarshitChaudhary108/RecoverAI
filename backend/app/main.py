from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.app.logging_setup import setup_logging
from backend.app.webhook import router as webhook_router
from backend.app.api import api_router

app = FastAPI(title="Payment Recovery Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    setup_logging()

@app.get("/health")
async def health_check():
    return {"status": "OK"}

app.include_router(webhook_router)
app.include_router(api_router, prefix="/api")
