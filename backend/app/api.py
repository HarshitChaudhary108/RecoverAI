from fastapi import APIRouter
from backend.app.stats import router as stats_router

api_router = APIRouter()
api_router.include_router(stats_router)
