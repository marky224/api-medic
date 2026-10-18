"""The package version has one source: api_medic.__version__."""

from __future__ import annotations

from importlib.metadata import version

from api_medic import __version__


def test_installed_metadata_matches_dunder_version():
    # pyproject.toml reads the version from src/api_medic/__init__.py
    # ([tool.hatch.version]), so the built package can't drift from it.
    assert version("api-medic") == __version__
