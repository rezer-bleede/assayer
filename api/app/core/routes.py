"""Route plumbing shared by every API router."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import log


class SafeAPIRoute(APIRoute):
    """Turn an unhandled exception into a real JSON response.

    Two reasons this matters, beyond tidiness:

    * an error body never leaks a stack trace or credential-bearing message;
    * Starlette's ``ServerErrorMiddleware`` sits *outside* ``CORSMiddleware``,
      so an exception that propagates all the way up produces a 500 with no
      ``Access-Control-Allow-Origin`` header — and the browser then reports a
      perfectly ordinary server bug as "blocked by CORS policy", which is
      exactly the class of ghost the user cannot debug. Returning the response
      here keeps the failure inside the middleware stack, where CORS applies.

    ``HTTPException``/``RequestValidationError`` are deliberately re-raised:
    FastAPI's own handlers render those (400/404/422) *around* this wrapper,
    with their detail messages intact. Swallowing them here would turn every
    deliberate 400 into an anonymous 500.

    Every router must set ``route_class=SafeAPIRoute`` *at construction*:
    ``app.include_router`` copies the already-built route objects, so assigning
    ``app.router.route_class`` afterwards does not reach module-level routers.
    """

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request) -> Any:
            try:
                return await original(request)
            except (StarletteHTTPException, RequestValidationError):
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("request failed", path=request.url.path)
                return ORJSONResponse(
                    status_code=500,
                    content={
                        "error": "internal_error",
                        "type": type(exc).__name__,
                        "message": str(exc)[:400],
                        "path": request.url.path,
                    },
                )

        return handler
