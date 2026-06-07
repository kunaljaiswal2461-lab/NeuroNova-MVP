from __future__ import annotations


class AppError(Exception):
    """Base for application errors that map to a known HTTP status."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ValidationFailure(AppError):
    status_code = 422
    code = "validation_error"


class UnsupportedFileTypeError(AppError):
    status_code = 415
    code = "unsupported_file_type"


class FileTooLargeError(AppError):
    status_code = 413
    code = "file_too_large"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
