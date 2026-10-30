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

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field
from pydantic_core import PydanticSerializationError

from .. import __version__
from ..core.engine import analyze
from ..core.models import UNENCODABLE_TEXT, Report, is_unencodable_text_error
from ..core.parser import parse_curl, parse_har
from ..core.runner import run as run_request
from ..core.runner_safety import InvalidRequestError, UnusableResponseError

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


@app.exception_handler(PydanticSerializationError)
@app.exception_handler(UnicodeEncodeError)
async def _unencodable_report(
    request: Request, exc: PydanticSerializationError | UnicodeEncodeError
) -> JSONResponse:
    # FastAPI serializes the returned Report, and an unpaired surrogate from
    # the input fails that: in pydantic's dump_json (PydanticSerializationError)
    # or, in older FastAPI, in JSONResponse's encode (UnicodeEncodeError).
    # Anything else is a bug and stays a 500.
    if not is_unencodable_text_error(exc):
        raise exc
    return JSONResponse(status_code=400, content={"detail": UNENCODABLE_TEXT})


@app.exception_handler(RequestValidationError)
async def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI's own 422 echoes each error's input, and jsonable_encoder
    # recurses into it: a value nested about 1,000 levels deep raised
    # RecursionError, a 500. Send the rest of each error without it, and
    # without a union tag error's ctx, which quotes the tag it found.
    errors = []
    for error in exc.errors():
        out = {key: value for key, value in error.items() if key != "input"}
        if out["type"] == "union_tag_invalid":
            out.pop("ctx", None)
        errors.append(out)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


def _non_empty(value: str) -> str:
    # Not Field(min_length=1): pydantic can't measure a str holding a lone
    # surrogate and would answer a 422 (string_unicode), where the hosted
    # demo's /api/run answers 400 naming the problem. Let it through; the
    # runner answers that 400.
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
    except (InvalidRequestError, UnusableResponseError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return analyze(captured)


@app.post("/api/analyze")
async def analyze_endpoint(body: AnalyzeRequest) -> Report:
    try:
        if isinstance(body, AnalyzeHarRequest):
            captured = parse_har(body.har)
        else:
            captured = parse_curl(body.curl)
        # analyze() raises ValueError for input it can't make a Report of,
        # e.g. a URL holding an unpaired surrogate.
        return analyze(captured)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
