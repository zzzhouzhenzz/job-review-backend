#!/usr/bin/env python3
"""Run the real Swift client through Caddy HTTPS against a fictional backend.

Requires macOS, Xcode's Swift toolchain, openssl, and an existing Caddy binary
passed with --caddy or JOB_REVIEW_CADDY. No software is downloaded or installed.
The ephemeral CA is trusted only inside the test client, never by the app or OS.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[1]


def run(command, *, env=None, timeout=180):
    """Reap this test's process group even when interrupted during a build."""
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
        if process.returncode:
            raise RuntimeError(f"Command failed ({process.returncode}): {command[0]}\n{output}")
        return output
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


def build_client():
    build = ROOT / ".build" / "https"
    env = os.environ.copy()
    env["CLANG_MODULE_CACHE_PATH"] = str(build / "clang")
    env["SWIFTPM_MODULECACHE_OVERRIDE"] = str(build / "clang")
    options = ["--package-path", str(ROOT), "--scratch-path", str(build / "swift"),
               "--cache-path", str(build / "cache"), "--disable-sandbox"]
    print("Building the separate Swift HTTPS check target.", flush=True)
    output = run(["swift", "build", *options, "--product", "ReviewHTTPSChecks"], env=env)
    print(output.rstrip(), flush=True)
    directory = run(["swift", "build", *options, "--show-bin-path"], env=env).strip()
    binary = Path(directory) / "ReviewHTTPSChecks"
    if not binary.is_file():
        raise RuntimeError("Swift did not produce ReviewHTTPSChecks")
    return binary


def seed(store):
    demo = json.loads((ROOT / "Sources/ReviewCore/Resources/demo-state.json").read_text())
    catalog = {"companies": [
        {"id": company["id"], "name": company["name"], **company["data"],
         "jobs": [{"job_id": job["job_id"], **job["data"]}
                  for job in demo["jobs"] if job["company_id"] == company["id"]]}
        for company in demo["companies"]]}
    store.import_catalog(catalog, "Fictional Swift HTTPS integration")
    card = next(card for card in store.feed()["cards"] if card["company"]["id"] == "northstar")
    ack = store.receive_decision({"request_id": "https-seed-apply", "action": "apply",
        "card_id": card["id"], "card_revision": card["revision"], "selected": [card["jobs"][0]["key"]]})
    process_id = ack["process_ids"][0]
    task = store.lease_task("fictional-https-seed", kinds=["apply"])
    store.start_application(process_id, task["lease_token"])
    store.ask_questions(process_id, task["lease_token"], "fictional-https-form-v1", [
        {"id": "https-text", "text": "Give a fictional test answer.", "type": "text",
         "required": True, "scope": "application"},
        {"id": "https-single", "text": "Choose a fictional office.", "type": "single_choice",
         "required": True, "scope": "application", "choices": [
             {"id": "north", "label": "North"}, {"id": "south", "label": "South"}]},
        {"id": "https-multi", "text": "Choose fictional interests.", "type": "multi_choice",
         "required": False, "scope": "application", "choices": [
             {"id": "infra", "label": "Infrastructure"}, {"id": "tools", "label": "Tools"}]}
    ])
    return process_id, task["id"]


def check_effects(store, result, seeded):
    def expect(condition, message):
        if not condition:
            raise RuntimeError(message)

    processes = store.processes()
    expect(len(processes) == 2, "HTTPS replay created an extra application process")
    applied = next(process for process in processes if process["id"] == result["apply_process_id"])
    expect(applied["job_key"] == result["selected_job_key"] == "canopy:demo-3",
           "HTTPS Apply did not save exactly the selected fictional job")
    expect(applied["task_status"] == "cancelled" and applied["submission_status"] == "not_started",
           "HTTPS cancellation changed the wrong state")
    question_process = next(process for process in processes if process["id"] == seeded[0])
    expect(question_process["id"] == result["question_process_id"]
           and question_process["task_id"] == seeded[1]
           and question_process["task_status"] == "queued"
           and question_process["submission_status"] == "preparing",
           "HTTPS answers did not resume the original task")
    expected = {"https-text": "Fictional HTTPS answer.", "https-single": "north",
                "https-multi": ["infra", "tools"]}
    questions = store.questions()
    expect({question["id"]: question["answer"] for question in questions} == expected,
           "HTTPS typed answers changed in backend storage")
    with store.connect() as database:
        commands = database.execute("SELECT id,request_body FROM wf_commands").fetchall()
        expect({row["id"] for row in commands} == {"https-seed-apply", result["apply_request_id"], result["cancel_request_id"]},
               "Unexpected command count after exact HTTPS replay")
        body = next(row["request_body"] for row in commands if row["id"] == result["apply_request_id"])
        expect(json.loads(body) == json.loads(base64.b64decode(result["apply_body_base64"], validate=True)),
               "Stored Apply command differs from the Swift pending bytes")
        answers = database.execute("SELECT id FROM wf_answer_requests").fetchall()
        expect([row["id"] for row in answers] == [result["answer_request_id"]], "Answer command was duplicated")
        expect(database.execute("SELECT COUNT(*) FROM wf_submission_attempts").fetchone()[0] == 0,
               "Fictional integration must never attempt employer submission")
    return "PASS Python storage audit: exact selection, one replayed process, typed answers, cancellation, zero submission attempts"


def integration(binary, caddy):
    sys.path.insert(0, str(ROOT.parent / "job-review"))
    from device_access import create_credentials
    from server import ReviewServer
    from test_tls_gateway import make_certificates, unused_port
    from tls_gateway import GatewayConfig, running_gateway

    with tempfile.TemporaryDirectory(prefix="jobswipe-swift-https-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        ca, certificate, key = make_certificates(root)
        for source, target in [(ca, root / "ca.der"), (certificate, root / "leaf.der")]:
            run([shutil.which("openssl"), "x509", "-in", str(source), "-outform", "DER", "-out", str(target)], timeout=20)
        port = unused_port()
        origin = f"https://localhost:{port}"
        access, credential = create_credentials(origin, root / "access.json", root / "credential.txt")
        secret = credential.read_text().strip()
        config = {"origin": origin, "credentialFile": str(credential), "caDER": str(root / "ca.der"),
                  "leafDER": str(root / "leaf.der"), "cacheDirectory": str(root / "cache"),
                  "resultFile": str(root / "result.json")}
        config_file = root / "client.json"
        config_file.write_text(json.dumps(config))
        config_file.chmod(0o600)
        server = None
        thread = None
        swift_output = ""
        with (root / "backend.log").open("w") as backend_log, (root / "caddy.log").open("wb") as gateway_log:
            try:
                with redirect_stdout(backend_log):
                    server = ReviewServer(("127.0.0.1", 0), root / "fictional.sqlite3", access_config=access)
                    seeded = seed(server.store)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    gateway = GatewayConfig(access_config=access, credential_file=credential,
                        cert_file=certificate, key_file=key, backend_port=server.server_port,
                        listen_address="127.0.0.1", listen_port=port, caddy=str(caddy), ca_file=ca)
                    with running_gateway(gateway, log_output=gateway_log):
                        swift_output = run([str(binary), str(config_file)], timeout=120)
                        result = json.loads((root / "result.json").read_text())
                        audit = check_effects(server.store, result, seeded)
            except Exception as error:
                # Test credentials must never appear in diagnostics, including third-party errors.
                raise RuntimeError(str(error).replace(secret, "[redacted]")
                                   .replace(server.token if server else secret, "[redacted]")) from None
            finally:
                if server is not None:
                    if thread is not None and thread.is_alive():
                        server.shutdown()
                    server.server_close()
                if thread is not None:
                    thread.join(timeout=5)
                    if thread.is_alive():
                        raise RuntimeError("Fictional backend thread did not stop")
        for path in [root / "backend.log", root / "caddy.log", root / "result.json", *list((root / "cache").glob("*.json"))]:
            content = path.read_bytes()
            if secret.encode() in content or server.token.encode() in content:
                raise RuntimeError("A test credential appeared in logs or durable output")
        if secret in swift_output or server.token in swift_output:
            raise RuntimeError("A test credential appeared in Swift diagnostics")
        print(swift_output.rstrip())
        print(audit)
        print("PASS temporary credentials absent from logs, results, and durable client cache")
    print("HTTPS integration passed. Caddy, backend, temporary database, certificates, and credentials were removed.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caddy", default=os.environ.get("JOB_REVIEW_CADDY"),
                        help="Existing Caddy executable (or set JOB_REVIEW_CADDY)")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.error("This check requires macOS Security.framework and URLSession")
    if not args.caddy:
        parser.error("Pass --caddy or JOB_REVIEW_CADDY; HTTPS checks are never silently skipped")
    caddy = shutil.which(args.caddy)
    if caddy is None:
        parser.error("The supplied Caddy executable is unavailable")
    if not shutil.which("swift") or not shutil.which("openssl"):
        parser.error("Swift and openssl are required; HTTPS checks are never silently skipped")

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        integration(build_client(), Path(caddy))
        return 0
    except KeyboardInterrupt:
        print("HTTPS verification interrupted; temporary services and files were cleaned up.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"HTTPS verification failed: {error}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
