from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()

from contextlib import asynccontextmanager
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware 
from fastapi.staticfiles import StaticFiles

from app.api.routes import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine
from app.exceptions.handlers import register_exception_handlers
from app.middlewares.request_id import RequestIDMiddleware
from app.middlewares.timing import TimingMiddleware

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    yield
    await dispose_engine()

def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="NeuroNova API", lifespan=lifespan)

    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(TimingMiddleware)
    app.add_middleware(RequestIDMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router)

    # Mount the visualization directory
    viz_path = os.path.join(settings.data_dir, "viz")
    os.makedirs(viz_path, exist_ok=True)
    app.mount("/viz", StaticFiles(directory=viz_path), name="viz_root")

    @app.get("/api/v1/datasets/{dataset_id}/charts", tags=["viz"])
    async def get_dataset_charts(dataset_id: str):
        settings = get_settings()
        viz_path = os.path.join(settings.data_dir, "viz")
        chart_urls = []
        
        if os.path.exists(viz_path):
            all_files = os.listdir(viz_path)
            print(f"DEBUG: Looking for charts in {viz_path}. Files found: {all_files}")
            
            for file in all_files:
                if file.lower().endswith(('.png', '.jpg', '.jpeg', '.svg')):
                    chart_urls.append(f"http://localhost:8000/viz/{file}")
        
        return {"charts": chart_urls}

    return app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    s = get_settings()
    uvicorn.run("main:app", host=s.app_host, port=s.app_port, reload=True)