"""Public API routes (catalog, session, gates). Filled in by S1.3 and S3.4."""

from __future__ import annotations

from fastapi.responses import JSONResponse

from backend import store

# HTTP status for each StoreError code. Unlisted codes are 400.
STORE_ERROR_STATUS = {
    "store_occupied": 409,
    "vision_unavailable": 503,
    "invalid_state": 409,
    "no_active_session": 409,
    "cart_not_empty": 409,
    "unknown_member": 404,
    "unknown_sku": 400,
}


class ApiError(Exception):
    """Raise from a route to answer {"error": code, "message": message} with `status`."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message, **extra})


def store_error_response(e: store.StoreError) -> JSONResponse:
    extra = {"occupant_first_name": e.occupant_first_name} if isinstance(e, store.StoreOccupied) else {}
    return error_response(STORE_ERROR_STATUS.get(e.code, 400), e.code, e.message, **extra)
