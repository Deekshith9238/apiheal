"""Trusted image harness. Run only in the apiheal container, never on target code on the host.

The contract uses the actual installed SDK against a loopback HTTP server. No
provider endpoint, real token, or repository-controlled test command is used.
"""

import contextlib
import importlib.metadata
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path("/workspace")
MOCK_TOKEN = "apiheal-test-token"
CASE_TEXTS = ["apiheal contract reply", "Unicode reply: नमस्ते 🌱"]


class MockAPI(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *arguments):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 100_000:
            self.send_error(413)
            return
        body = json.loads(self.rfile.read(length))
        self.server.requests.append(
            {
                "path": self.path,
                "body": body,
                "authenticated": self.headers.get("Authorization") == f"Bearer {MOCK_TOKEN}",
            }
        )
        status = self.server.status
        if status == 200:
            payload = {
                "id": "chatcmpl-apiheal",
                "object": "chat.completion",
                "created": 1,
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": self.server.reply,
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        else:
            payload = {
                "error": {
                    "message": "Synthetic invalid request",
                    "type": "invalid_request_error",
                    "param": "messages",
                    "code": "synthetic_invalid_request",
                }
            }
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def load_target(relative):
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT) or path.suffix != ".py":
        raise ValueError("Invalid module path")
    spec = importlib.util.spec_from_file_location("_apiheal_target", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inventory():
    return {
        distribution.metadata["Name"].lower(): distribution.version
        for distribution in importlib.metadata.distributions()
    }


def dependencies():
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        text = line.split(" #", 1)[0].strip()
        if not text or text.startswith("#"):
            continue
        requirement = Requirement(text)
        if requirement.url or requirement.extras or requirement.marker:
            raise ValueError("Unsupported dependency layout")
        version = importlib.metadata.version(requirement.name)
        if version not in requirement.specifier:
            raise AssertionError(f"Unsatisfied prepared dependency: {requirement.name}")
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "pip", "check"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode:
        raise AssertionError("Prepared dependencies failed pip check")


def syntax_imports(files):
    for file in files:
        path = ROOT / file
        compile(path.read_bytes(), str(path), "exec")
    for file in files:
        load_target(file)


def contract(targets, expected_failure=False):
    # Import the image SDK before putting repository modules on sys.path.
    import openai

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockAPI)
    server.daemon_threads = True
    server.requests = []
    server.status, server.reply = 200, CASE_TEXTS[0]
    address = f"http://127.0.0.1:{server.server_port}/v1"
    os.environ.update(OPENAI_API_KEY=MOCK_TOKEN, OPENAI_BASE_URL=address)
    if importlib.metadata.version("openai").startswith("0."):
        openai.api_key, openai.api_base = MOCK_TOKEN, address
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    sys.path.insert(0, str(ROOT))
    facts = []
    try:
        for target in targets:
            module = load_target(target["file"])
            function = getattr(module, target["function"])
            inputs = {
                "model": "apiheal-contract-model",
                "messages": [
                    {"role": "user", "content": "Synthetic request; no provider call"},
                ],
            }
            if expected_failure:
                try:
                    function(**inputs)
                except Exception as error:
                    if type(error).__name__ != "APIRemovedInV1" or not type(
                        error
                    ).__module__.startswith("openai."):
                        raise AssertionError("Baseline failed for an unexpected reason") from error
                else:
                    raise AssertionError(
                        "Original source did not reproduce the expected SDK failure"
                    )
                if server.requests:
                    raise AssertionError("Removed API unexpectedly reached the HTTP endpoint")
                facts.append({"target": target, "exception": "APIRemovedInV1"})
                continue
            for index, reply in enumerate([*CASE_TEXTS, None]):
                server.status, server.reply = (400, "") if reply is None else (200, reply)
                server.requests.clear()
                error_status = None
                try:
                    returned = function(**inputs)
                except Exception as error:
                    error_status = getattr(
                        error, "status_code", getattr(error, "http_status", None)
                    )
                    if reply is not None or error_status != 400:
                        raise
                    returned = None
                else:
                    if reply is None or returned != reply:
                        raise AssertionError("Response extraction/error propagation changed")
                if len(server.requests) != 1:
                    raise AssertionError("Expected exactly one request per case")
                request = server.requests[0]
                if (
                    request["path"] != "/v1/chat/completions"
                    or not request["authenticated"]
                    or request["body"].get("model") != inputs["model"]
                    or request["body"].get("messages") != inputs["messages"]
                ):
                    raise AssertionError("Request parameters, endpoint, or authentication changed")
                facts.append(
                    {
                        "target": target,
                        "case": index,
                        "request": request,
                        "returned": returned,
                        "error_status": error_status,
                    }
                )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    return facts


def unit_tests(files):
    report = Path("/tmp/unit.xml")
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys,openai,pytest;sys.path.insert(0,'/workspace');"
            "raise SystemExit(pytest.main(sys.argv[1:]))",
            "-c",
            "/opt/apiheal/pytest.ini",
            "--import-mode=importlib",
            "--junitxml",
            str(report),
            *[str(ROOT / f) for f in files],
        ],
        capture_output=True,
        text=True,
        timeout=40,
        env={
            **os.environ,
            "OPENAI_API_KEY": MOCK_TOKEN,
            "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
            "OPENAI_API_BASE": "http://127.0.0.1:9/v1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
    )
    if not report.is_file():
        raise AssertionError("Unit tests produced no evidence")
    root = ET.parse(report).getroot()
    cases = list(root.iter("testcase"))
    count = len(cases)
    skipped_or_failed = any(
        list(case)
        for case in cases
        if any(child.tag in {"skipped", "failure", "error"} for child in case)
    )
    if completed.returncode or count == 0 or skipped_or_failed:
        raise AssertionError(
            "Unit tests failed, skipped required cases, or executed zero tests. "
            + completed.stdout[-1000:]
            + completed.stderr[-500:]
        )
    return count


def run(request):
    os.environ.update(
        OPENAI_API_KEY=MOCK_TOKEN,
        OPENAI_BASE_URL="http://127.0.0.1:9/v1",
        OPENAI_API_BASE="http://127.0.0.1:9/v1",
    )
    action = request["action"]
    if action == "dependencies":
        dependencies()
        return {"test_count": None, "facts": []}
    if action == "syntax_imports":
        syntax_imports(request["files"])
        return {"test_count": None, "facts": []}
    if action == "unit":
        return {"test_count": unit_tests(request["files"]), "facts": []}
    facts = contract(request["targets"], expected_failure=action == "expected_failure")
    return {"test_count": len(facts), "facts": facts}


def main():
    request = json.loads(Path("/input/request.json").read_text())
    payload = {
        "schema_version": 1,
        "action": request["action"],
        "outcome": "failed",
        "test_count": None,
        "facts": [],
        "dependencies": inventory(),
    }
    try:
        # Repository prints are not allowed to forge the single JSON result envelope.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            payload.update(run(request))
        payload["outcome"] = "passed"
    except Exception as error:
        payload["error"] = f"{type(error).__name__}: {str(error)[:1000]}"
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["outcome"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
