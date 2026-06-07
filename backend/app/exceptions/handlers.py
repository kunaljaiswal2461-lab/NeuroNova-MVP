from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger
from app.exceptions.custom_exceptions import AppError
from app.schemas.error_schemas import ErrorPayload, ErrorResponse


logger = get_logger("errors")


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _error_response(
    status: int,
    code: str,
    message: str,
    details: dict | None,
    request_id: str | None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorPayload(
            code=code,
            message=message,
            details=details or {},
            request_id=request_id,
        )
    )
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError):
        logger.warning(
            "app_error",
            code=exc.code,
            status=exc.status_code,
            message=exc.message,
            details=exc.details,
        )
        return _error_response(
            exc.status_code, exc.code, exc.message, exc.details, _request_id(request)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError):
        return _error_response(
            422,
            "validation_error",
            "request validation failed",
            {"errors": exc.errors()},
            _request_id(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        return _error_response(
            exc.status_code,
            "http_error",
            str(exc.detail),
            None,
            _request_id(request),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception):
        logger.exception("unhandled_error", error=str(exc))
        return _error_response(
            500,
            "internal_error",
            "internal server error",
            None,
            _request_id(request),
        )
