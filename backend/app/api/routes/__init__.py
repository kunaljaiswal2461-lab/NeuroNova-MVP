from fastapi import APIRouter

from app.api.routes.artifact_routes import router as artifact_router
from app.api.routes.auth_routes import router as auth_router
from app.api.routes.chat_routes import router as chat_router
from app.api.routes.dataset_routes import router as dataset_router
from app.api.routes.health_routes import router as health_router
from app.api.routes.source_routes import router as source_router


api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(dataset_router)
api_router.include_router(chat_router)
api_router.include_router(artifact_router)
api_router.include_router(source_router)

__all__ = ["api_router"]
