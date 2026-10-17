#!/usr/bin/env python3
"""Start the local job inbox, or report an existing instance."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent


def probe(port):
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1) as response:
            state = json.load(response)
        if all(isinstance(state.get(key), list) for key in ("companies", "jobs", "queue")):
            return "ready"
        return "other"
    except HTTPError as error:
        return "protected" if error.code in (401, 403) else "other"
    except (URLError, TimeoutError, OSError):
        return "unavailable"
    except (ValueError, TypeError):
        return "other"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    state = probe(args.port)
    if state == "ready":
        print(f"Job inbox is running: http://127.0.0.1:{args.port}")
        return
    if state == "other":
        raise SystemExit("Another app uses this port. Stop that app or choose a different port.")
    if state == "protected":
        raise SystemExit("A server on this port requires authentication. Use its HTTPS address and device credential in the iOS app. This launcher starts local web mode only.")
    state_dir = ROOT / "state"
    state_dir.mkdir(exist_ok=True)
    log_path = state_dir / "server.log"
    with log_path.open("ab") as log:
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py"), "--port", str(args.port)],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,
        )
    for _ in range(30):
        if child.poll() is not None:
            raise SystemExit(f"The server stopped. Read {log_path}")
        if probe(args.port) == "ready":
            (state_dir / "server.pid").write_text(str(child.pid) + "\n")
            print(f"Job inbox: http://127.0.0.1:{args.port}")
            print(f"Server log: {log_path}")
            return
        time.sleep(0.1)
    child.terminate()
    child.wait(timeout=5)
    raise SystemExit(f"The server did not become ready. Read {log_path}")


if __name__ == "__main__":
    main()
