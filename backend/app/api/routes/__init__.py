from fastapi import APIRouter

from app.api.routes.chat_routes import router as chat_router
from app.api.routes.dataset_routes import router as dataset_router


api_router = APIRouter()
api_router.include_router(dataset_router)
api_router.include_router(chat_router)

__all__ = ["api_router"]
