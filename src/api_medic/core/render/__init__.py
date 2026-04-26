"""Report renderers.

Phase 3a ships JSON only. Terminal (rich), Markdown, and HTML renderers
land in Phase 3b alongside the rest of the engine work.
"""

from .json import render_json

__all__ = ["render_json"]
