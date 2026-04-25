"""Export the Report JSON Schema to stdout.

Run via `make types` to feed quicktype and regenerate the TypeScript types.

Usage:
    python -m api_medic.core.export_schema > schema.json
"""

from __future__ import annotations

import json
import sys

from api_medic.core.models import Report


def main() -> None:
    schema = Report.model_json_schema()
    json.dump(schema, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
