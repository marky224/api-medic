"""FastAPI app powering the local web UI and (Phase 5) the hosted demo.

Routes:
  * GET  /api/health   — liveness check
  * POST /api/run      — execute via Runner (live mode, local dev only)
  * POST /api/analyze  — Parser + engine (captured mode, also used by Lambda)

The Lambda surface for the hosted demo will only mount /api/analyze and
/api/health — it deliberately doesn't expose the live runner. Phase 5 work.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..core.engine import analyze
from ..core.models import Report
from ..core.parser import parse_curl, parse_har
from ..core.runner import run as run_request

app = FastAPI(title="api-medic", version="0.1")

# Local Vite dev server. Phase 5 deploy will add the production hosted-demo
# origin via env config.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    method: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)
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
    captured = run_request(
        method=body.method,
        url=body.url,
        headers=body.headers,
        body=body.body,
    )
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
