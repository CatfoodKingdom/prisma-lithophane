"""Maintainer CLI for Prisma Standard Model Library releases."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Prisma.lib.standard_model_library import (  # noqa: E402
    StandardModelLibraryError,
    export_standard_model_library,
    validate_standard_model_library,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", help="build a new database-free model library")
    export.add_argument("--data-root", required=True, type=Path)
    export.add_argument("--sqlite", required=True, type=Path)
    export.add_argument("--destination", required=True, type=Path)
    export.add_argument("--library-name", required=True)
    export.add_argument("--library-version", required=True)
    export.add_argument("--publisher", required=True)
    export.add_argument("--minimum-prisma-version", required=True)
    export.add_argument("--maximum-prisma-version")
    export.add_argument("--description", default="")
    export.add_argument("--release-notes", default="")
    export.add_argument("--library-id", help="canonical UUID; generated when omitted")

    validate = commands.add_parser("validate", help="validate an extracted model library")
    validate.add_argument("--library-root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "export":
            report = export_standard_model_library(
                data_root=args.data_root,
                sqlite_path=args.sqlite,
                destination=args.destination,
                library_name=args.library_name,
                library_version=args.library_version,
                publisher=args.publisher,
                minimum_prisma_version=args.minimum_prisma_version,
                maximum_prisma_version=args.maximum_prisma_version,
                description=args.description,
                release_notes=args.release_notes,
                library_id=args.library_id,
            )
        else:
            report = validate_standard_model_library(args.library_root)
    except StandardModelLibraryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
