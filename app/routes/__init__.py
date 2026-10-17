from fastapi import APIRouter

from app.routes import auth
from app.routes import yt_management

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(yt_management.router)