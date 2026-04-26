"""Check registry.

A check is a function `(CapturedRequest) -> Finding | list[Finding] | None`.
Returning `None` means the check didn't fire — return only when something
is actually wrong (or worth reporting as info).

Each check module under `core.checks` registers its functions on import. The
engine glue (core.engine) imports those modules so registration happens
before `run_all_checks` is called.

Tests that want isolation can instantiate their own `CheckRegistry` rather
than touching `default_registry`.
"""

from __future__ import annotations

from collections.abc import Callable

from ..captured import CapturedRequest
from ..models import Finding

CheckResult = Finding | list[Finding] | None
CheckFn = Callable[[CapturedRequest], CheckResult]


class CheckRegistry:
    def __init__(self) -> None:
        self._checks: list[CheckFn] = []

    def register(self, fn: CheckFn) -> CheckFn:
        self._checks.append(fn)
        return fn

    def all(self) -> list[CheckFn]:
        return list(self._checks)

    def run(self, captured: CapturedRequest) -> list[Finding]:
        out: list[Finding] = []
        for fn in self._checks:
            result = fn(captured)
            if result is None:
                continue
            if isinstance(result, list):
                out.extend(result)
            else:
                out.append(result)
        return out


default_registry = CheckRegistry()
register = default_registry.register
run_all_checks = default_registry.run
