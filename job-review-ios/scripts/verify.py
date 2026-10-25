#!/usr/bin/env python3
"""Build the shared SwiftUI code and test the core against an isolated Python server."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-integration", action="store_true", help="Skip the isolated loopback HTTP test.")
    args = parser.parse_args()
    env = os.environ.copy()
    env.pop("JOB_REVIEW_TEST_SERVER_URL", None)
    env.pop("JOB_REVIEW_WORKFLOW_TEST_SERVER_URL", None)
    env["JOB_REVIEW_WORKFLOW_FIXTURES"] = str(ROOT.parent / "job-review/fixtures/v1-http.json")
    build = ROOT / ".build"
    env["CLANG_MODULE_CACHE_PATH"] = str(build / "clang")
    env["SWIFTPM_MODULECACHE_OVERRIDE"] = str(build / "clang")
    options = ["--package-path", str(ROOT), "--scratch-path", str(build / "swift"),
               "--cache-path", str(build / "cache"), "--disable-sandbox"]
    subprocess.run(["swift", "build", *options, "--product", "JobReviewPreview"], env=env, check=True)
    command = ["swift", "run", *options, "ReviewCoreChecks"]

    with tempfile.TemporaryDirectory(prefix="job-review-ios-test-") as temp:
        servers = []
        threads = []
        if not args.no_integration:
            backend = ROOT.parent / "job-review"
            sys.path.insert(0, str(backend))
            from server import ReviewServer
            demo = json.loads((ROOT / "Sources/ReviewCore/Resources/demo-state.json").read_text())
            catalog = {"companies": [{"id": company["id"], "name": company["name"], **company["data"],
                                     "jobs": [{"job_id": job["job_id"], **job["data"]} for job in demo["jobs"] if job["company_id"] == company["id"]]}
                                    for company in demo["companies"]]}
            for name, variable in [("legacy", "JOB_REVIEW_TEST_SERVER_URL"),
                                   ("workflow", "JOB_REVIEW_WORKFLOW_TEST_SERVER_URL")]:
                server = ReviewServer(("127.0.0.1", 0), Path(temp) / f"{name}.sqlite3")
                server.store.import_catalog(catalog, "iOS isolated contract test")
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                servers.append(server)
                threads.append(thread)
                env[variable] = f"http://127.0.0.1:{server.server_port}"
            print("Testing with fictional jobs in a temporary database.", flush=True)
        try:
            return subprocess.call(command, env=env)
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
