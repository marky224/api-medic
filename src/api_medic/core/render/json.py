"""JSON renderer for Reports.

Thin wrapper around Pydantic's `model_dump_json`. Exists so all renderers
(JSON, terminal, markdown, HTML) share the same call signature.
"""

from __future__ import annotations

from ..models import Report


def render_json(report: Report, *, indent: int | None = 2) -> str:
    """Render a Report as a JSON string. `indent=None` produces compact output."""
    return report.model_dump_json(indent=indent)
