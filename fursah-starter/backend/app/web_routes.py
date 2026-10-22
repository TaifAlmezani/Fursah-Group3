"""Team addition: POST /api/v1/web-search, the user-approved web search.

A separate endpoint, so the supplied discovery and details contracts stay unchanged.
"""
from __future__ import annotations

import time
from dataclasses import replace
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .infrastructure.dependencies import AppDependencies, get_app_dependencies
from .infrastructure.errors import ErrorCode, FursahError
from .infrastructure.tracing import Tracer
from .schemas import ApiErrorResponse, DiscoveryRequest
from .web_search import WebSearchResponse, web_discover

router = APIRouter()


@router.post("/api/v1/web-search", response_model=WebSearchResponse)
async def web_search_route(
    body: DiscoveryRequest,
    request: Request,
    deps: AppDependencies = Depends(get_app_dependencies),
):
    deps = replace(deps, tracer=Tracer(deps.settings.trace_jsonl_path),
                   evaluation_time=deps.settings.resolve_evaluation_time())
    request_id = body.request_id or (request.headers.get("x-request-id") or "").strip()[:120] or str(uuid4())
    body = body.model_copy(update={"request_id": request_id})
    started = time.perf_counter()
    try:
        result = await web_discover(body, deps)
    except FursahError as exc:
        return _failed(deps, request_id, started, exc.public_code, exc.public_message,
                       exc.http_status, f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # never let an unexpected error reach the browser without CORS
        return _failed(deps, request_id, started, ErrorCode.INTERNAL,
                       "The web search could not be completed.", 503, type(exc).__name__)
    deps.tracer.emit(request_id=request_id, step="web_search_ready", component="web",
                     outcome=result.status, elapsed_ms=(time.perf_counter() - started) * 1000)
    return result


def _failed(deps, request_id: str, started: float, code: str, message: str,
            status_code: int, reason: str) -> JSONResponse:
    deps.tracer.emit(request_id=request_id, step="web_search_failed", component="web",
                     outcome=reason[:300], elapsed_ms=(time.perf_counter() - started) * 1000)
    payload = ApiErrorResponse(request_id=request_id, error_code=code, message=message)
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))
