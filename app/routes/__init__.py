# app/routes/__init__.py
from fastapi import APIRouter

from app.routes import auth
from app.routes import google_credentials
from app.routes import storage_bucket
from app.routes import yt_channel_management
from app.routes import yt_management

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(yt_management.router)
api_router.include_router(google_credentials.router)
api_router.include_router(storage_bucket.router)
api_router.include_router(yt_channel_management.router)