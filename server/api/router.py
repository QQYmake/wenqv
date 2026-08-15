"""Combined API router."""

from fastapi import APIRouter

from .chat import router as chat_router
from .files import router as files_router
from .meta import router as meta_router
from .provider import router as provider_router


api_router = APIRouter(prefix="/api")
api_router.include_router(chat_router)
api_router.include_router(files_router)
api_router.include_router(meta_router)
api_router.include_router(provider_router)

__all__ = ["api_router"]
