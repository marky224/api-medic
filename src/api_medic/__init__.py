"""api-medic — diagnostic tool for HTTP API issues."""

# The one place the version lives. pyproject.toml reads it at build time
# ([tool.hatch.version]); the CLI, web app, Lambda and runner import it.
# The hosted-demo Lambda copies this source without installing the
# package, so importlib.metadata would find no version there.
__version__ = "1.1.1"
