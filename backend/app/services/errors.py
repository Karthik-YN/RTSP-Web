"""Service-layer errors mapped to HTTP responses by the API layer."""

from __future__ import annotations


class ServiceError(Exception):
    status_code = 400

    def __init__(self, message: str, details: dict | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code


class NotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    status_code = 409


class Invalid(ServiceError):
    status_code = 422


class CapacityExceeded(ServiceError):
    status_code = 409
