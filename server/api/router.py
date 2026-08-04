"""Combined API router."""

from fastapi import APIRouter

from .chat import router as chat_router
from .meta import router as meta_router
from .sessions import router as sessions_router


api_router = APIRouter(prefix="/api")
api_router.include_router(chat_router)
api_router.include_router(sessions_router)
api_router.include_router(meta_router)

__all__ = ["api_router"]
