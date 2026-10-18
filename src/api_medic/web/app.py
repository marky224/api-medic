"""FastAPI app powering the local web UI.

Routes:
  * GET  /api/health   — liveness check
  * POST /api/run      — execute via Runner (live mode)
  * POST /api/analyze  — Parser + engine (captured mode)

The hosted-demo Lambda mounts the same three routes (see deploy/lambda/
handler.py). It dispatches them inline rather than via FastAPI to keep
the cold-start budget intact, but the route shapes match by design.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import AfterValidator, BaseModel, Field

from .. import __version__
from ..core.engine import analyze
from ..core.models import Report
from ..core.parser import parse_curl, parse_har
from ..core.runner import run as run_request
from ..core.runner_safety import InvalidRequestError

app = FastAPI(title="api-medic", version=__version__)

# Local Vite dev server. Phase 5 deploy will add the production hosted-demo
# origin via env config.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _non_empty(value: str) -> str:
    # Not Field(min_length=1): pydantic can't measure a str holding a lone
    # surrogate, and FastAPI's 422 for that echoes it back and fails to
    # encode (a 500). Let it through; the runner answers 400.
    if not value:
        raise ValueError("must not be empty")
    return value


NonEmptyStr = Annotated[str, AfterValidator(_non_empty)]


class RunRequest(BaseModel):
    method: NonEmptyStr
    url: NonEmptyStr
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None


class AnalyzeHarRequest(BaseModel):
    kind: Literal["har"]
    har: dict[str, Any]


class AnalyzeCurlRequest(BaseModel):
    kind: Literal["curl"]
    curl: str


AnalyzeRequest = Annotated[
    AnalyzeHarRequest | AnalyzeCurlRequest,
    Field(discriminator="kind"),
]


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": app.version}


@app.post("/api/run")
async def run_endpoint(body: RunRequest) -> Report:
    try:
        captured = run_request(
            method=body.method,
            url=body.url,
            headers=body.headers,
            body=body.body,
        )
    except InvalidRequestError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return analyze(captured)


@app.post("/api/analyze")
async def analyze_endpoint(body: AnalyzeRequest) -> Report:
    try:
        if isinstance(body, AnalyzeHarRequest):
            captured = parse_har(body.har)
        else:
            captured = parse_curl(body.curl)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return analyze(captured)
