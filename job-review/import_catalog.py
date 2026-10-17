#!/usr/bin/env python3
"""Import research or update a saved local queue. Never apply for a job."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from server import DEFAULT_DB
from store import ReviewError
from workflow import WorkflowStore as Store


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", nargs="?", type=Path, help="Catalog JSON file, or old data.json with --migrate.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--source", help="Optional label for this import; migration always uses the resolved catalog path.")
    parser.add_argument("--migrate", action="store_true", help="Import the old catalog and decisions once.")
    parser.add_argument("--decisions", type=Path, help="Original dated decisions directory for migration.")
    parser.add_argument("--status", choices=["in_progress", "needs_input", "applied", "failed", "done", "cancelled"])
    parser.add_argument("--queue-id")
    parser.add_argument("--note", default="")
    args = parser.parse_args(argv)
    try:
        store = Store(args.db)
        if args.status:
            if not args.queue_id or args.catalog or args.migrate or args.decisions:
                raise ReviewError("Use --status with --queue-id, without a catalog or migration options.")
            state = store.update_status(args.queue_id, args.status, args.note)
            result = next(item for item in state["queue"] if item["id"] == args.queue_id)
        else:
            if not args.catalog or args.queue_id:
                raise ReviewError("Give a catalog file, or use --status with --queue-id.")
            catalog = read_json(args.catalog)
            if args.migrate:
                draft = submitted = None
                if args.decisions:
                    resolved = args.decisions.resolve()
                    temporary = (Path("/tmp").resolve(), Path("/private/tmp").resolve(), Path("/private/var/folders").resolve())
                    if any(resolved.is_relative_to(path) for path in temporary) or any("qa" in part.lower() for part in resolved.parts):
                        raise ReviewError("Do not migrate QA or temporary decision files as approvals.")
                    if not resolved.is_dir():
                        raise ReviewError("The original decisions directory does not exist.")
                    for filename in ("draft.json", "submitted-review.json"):
                        path = resolved / filename
                        if path.exists():
                            if filename == "draft.json":
                                draft = read_json(path)
                            else:
                                submitted = read_json(path)
                result = store.migrate(catalog, "legacy:" + str(args.catalog.resolve()), draft, submitted)
            else:
                if args.decisions:
                    raise ReviewError("Use --decisions only with --migrate.")
                result = store.import_catalog(catalog, args.source or str(args.catalog.resolve()))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ReviewError, OSError, ValueError) as error:
        print(f"Import failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
