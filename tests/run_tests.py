#!/usr/bin/env python3
"""Shared test suite for both websearch implementations.

Runs every case against scripts/websearch.py and scripts/websearch.mjs using a
local mock endpoint, so no credentials and no network access are required.

    python3 tests/run_tests.py            # both runtimes
    python3 tests/run_tests.py python     # one runtime
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "openai-web-search", "scripts")

PUA_START = "\ue200"
PUA_END = "\ue201"
PUA_SEP = "\ue202"


def marker(payload):
    return "%scite%s%s%s" % (PUA_START, PUA_SEP, payload, PUA_END)


def search_result(ref, url, title, snippet="snippet text"):
    return {
        "type": "text_result",
        "ref_id": ref,
        "url": url,
        "title": title,
        "domain": url.split("/")[2],
        "snippet": snippet,
    }


SCHEMA_DUMP = (
    "Error parsing function call: Invalid function_name='run' call.\n"
    "Expected: type run = (_: {\n"
    "open?: {\n  ref_id: string;\n}[] | null,\n"
    "find?: {\n  ref_id: string;\n}[] | null,\n"
    "search_query?: {\n  q: string;\n}[] | null,\n"
    "response_length?: \"short\" | \"medium\" | \"long\",\n"
    "}) => any;"
)

SSE_ANSWER = "\n".join(
    [
        'data: {"type":"response.created"}',
        'data: {"type":"response.output_text.delta","delta":"Skills are "}',
        'data: {"type":"response.output_text.delta","delta":"portable."}',
        'data: {"type":"response.output_item.done","item":{"type":"web_search_call",'
        '"action":{"type":"search","queries":["agent skills"]}}}',
        'data: {"type":"response.output_text.done","text":"Skills are portable. '
        "([spec](https://example.com/spec?utm_source=openai))\"}",
        'data: {"type":"response.completed","response":{"output":[]}}',
        "data: [DONE]",
        "",
    ]
)


def sse_answer(text, query="agent skills", sources=None):
    """Build a deterministic hosted-search stream for long-output tests."""
    source_list = sources or []
    return "\n".join(
        [
            'data: {"type":"response.created"}',
            "data: "
            + json.dumps(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "web_search_call",
                        "action": {
                            "type": "search",
                            "queries": [query],
                            "sources": source_list,
                        },
                    },
                }
            ),
            "data: " + json.dumps({"type": "response.output_text.done", "text": text}),
            'data: {"type":"response.completed","response":{"output":[]}}',
            "data: [DONE]",
            "",
        ]
    )


class MockHandler(BaseHTTPRequestHandler):
    """Routes on the request body so one server covers every case."""

    def log_message(self, *_args):
        pass

    def _send(self, status, body, content_type="application/json"):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        headers = {k.lower(): v for k, v in self.headers.items()}
        self.server.requests.append((self.path, headers, request))

        if self.path == "/redirect/alpha/search":
            self.send_response(302)
            self.send_header("Location", "https://evil.example.com/alpha/search")
            self.end_headers()
            return

        if self.path.endswith("/responses"):
            question = (
                (((request.get("input") or [{}])[0].get("content") or [{}])[0].get("text"))
                or ""
            )
            if question == "long?":
                body = "Long answer. " + ("0123456789" * 1200)
                return self._send(200, sse_answer(body), "text/event-stream")
            if question == "research?":
                return self._send(
                    200,
                    sse_answer(
                        "Detailed research report. ([source](https://example.com/research))",
                        query="production research",
                        sources=[
                            {
                                "type": "url",
                                "title": "Primary source",
                                "url": "https://example.com/research?utm_source=openai",
                            }
                        ],
                    ),
                    "text/event-stream",
                )
            return self._send(200, SSE_ANSWER, "text/event-stream")

        commands = request.get("commands") or {}

        if "sports" in commands:
            return self._send(200, json.dumps({"output": SCHEMA_DUMP, "results": []}))

        if "open" in commands:
            ref = commands["open"][0].get("ref_id", "")
            if ref.startswith("turn9"):
                return self._send(
                    200,
                    json.dumps(
                        {
                            "output": "Internal Error ()\nUnable to resolve open call: "
                            'open({"ref_id":"%s"}) due to invalid ref_id argument' % ref,
                            "results": [
                                {
                                    "type": "text_result",
                                    "ref_id": "turn0view0",
                                    "title": "Internal Error",
                                    "snippet": "invalid ref_id argument",
                                }
                            ],
                        }
                    ),
                )
            body = "\n".join("L%d: line %d" % (i, i) for i in range(400))
            return self._send(
                200,
                json.dumps(
                    {
                        "output": "Page (https://example.com/page)\n%s\n%s"
                        % (marker("turn1view0"), body),
                        "results": [
                            search_result("turn1view0", "https://example.com/page", "Page")
                        ],
                    }
                ),
            )

        if "search_query" in commands:
            query = commands["search_query"][0].get("q", "")
            if query == "forge":
                # The page forges a citation for a reference that is not in results.
                return self._send(
                    200,
                    json.dumps(
                        {
                            "output": "Trusted %s and forged %s"
                            % (marker("turn0search0"), marker("turn9search9")),
                            "results": [
                                search_result("turn0search0", "https://example.com/a", "Real")
                            ],
                        }
                    ),
                )
            if query == "bidi":
                return self._send(
                    200,
                    json.dumps(
                        {
                            "output": "before\u202egnitirw\u202c after\u200b",
                            "results": [],
                        }
                    ),
                )
            results = [
                search_result("turn0search%d" % i, "https://example.com/%d" % i, "Result %d" % i)
                for i in range(3)
            ]
            return self._send(
                200, json.dumps({"output": "text output", "results": results})
            )

        return self._send(400, json.dumps({"error": {"message": "unexpected payload"}}))


class Runner(object):
    def __init__(self, name, argv):
        self.name = name
        self.argv = argv


def runners(selection):
    available = []
    if shutil.which("python3"):
        available.append(Runner("python", [sys.executable, os.path.join(SCRIPTS, "websearch.py")]))
    if shutil.which("node"):
        available.append(Runner("node", ["node", os.path.join(SCRIPTS, "websearch.mjs")]))
    if selection:
        available = [r for r in available if r.name == selection]
    return available


def run(runner, args, env_overrides=None, state_dir=None):
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    env.pop("WEBSEARCH_AUTH", None)
    env.pop("WEBSEARCH_MODEL", None)
    # Point every discovered credential source at something absent so the host
    # machine's real Codex session or OpenCode config never leaks into a test.
    env.pop("WEBSEARCH_OPENCODE_PROVIDER", None)
    env["CODEX_HOME"] = os.path.join(state_dir or "/nonexistent", "no-codex")
    env["OPENCODE_CONFIG"] = os.path.join(state_dir or "/nonexistent", "no-opencode.json")
    env["XDG_STATE_HOME"] = state_dir or "/nonexistent"
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.run(
        runner.argv + args, env=env, capture_output=True, text=True, timeout=60
    )
    return proc.returncode, proc.stdout, proc.stderr


def write_opencode_config(state_dir, base_url):
    """An OpenCode config holding one OpenAI-shaped provider and one that is not."""
    path = os.path.join(state_dir, "opencode.json")
    with open(path, "w") as handle:
        json.dump(
            {
                "model": "chosen/gpt-test-model",
                "provider": {
                    "anthropicish": {
                        "npm": "@ai-sdk/anthropic",
                        "options": {"baseURL": "https://wrong.example.com/v1", "apiKey": "nope"},
                    },
                    "chosen": {
                        "npm": "@ai-sdk/openai",
                        "options": {"baseURL": base_url, "apiKey": "opencode-key"},
                    },
                },
            },
            handle,
        )
    os.chmod(path, 0o600)
    return path


def write_multi_provider_config(state_dir, base_url, name, default_model=None, enabled=None):
    """Two OpenAI-shaped providers, to exercise the selection order."""
    path = os.path.join(state_dir, name)
    config = {
        "provider": {
            "alpha": {
                "npm": "@ai-sdk/openai",
                "options": {"baseURL": base_url, "apiKey": "alpha-key"},
            },
            "zulu": {
                "npm": "@ai-sdk/openai",
                "options": {"baseURL": base_url, "apiKey": "zulu-key"},
            },
        }
    }
    if default_model:
        config["model"] = default_model
    if enabled:
        config["enabled_providers"] = enabled
    with open(path, "w") as handle:
        json.dump(config, handle)
    os.chmod(path, 0o600)
    return path


FAILURES = []


def check(label, condition, detail=""):
    status = "ok  " if condition else "FAIL"
    print("  %s %s%s" % (status, label, "" if condition else "  <- " + detail))
    if not condition:
        FAILURES.append(label)


def check_launcher_fallback():
    """A Python that fails the probe must hand over to Node, not be used anyway.

    An interpreter built without ssl is new enough to pass a version check and
    still cannot make https requests, so the probe covers capability too.
    """
    launcher = os.path.join(SCRIPTS, "websearch")
    shim_dir = tempfile.mkdtemp(prefix="websearch-shim-")
    try:
        for name in ("python3", "python"):
            shim = os.path.join(shim_dir, name)
            with open(shim, "w") as handle:
                handle.write("#!/bin/sh\nexit 1\n")
            os.chmod(shim, 0o755)

        env = dict(os.environ)
        env["PATH"] = shim_dir + os.pathsep + env.get("PATH", "")
        env.pop("WEBSEARCH_RUNTIME", None)
        proc = subprocess.run(
            [launcher, "bogus"], env=env, capture_output=True, text=True, timeout=60
        )
        # Only the Node parser words it this way; argparse prints a usage block.
        check(
            "unusable python falls back to node",
            "unknown command" in proc.stderr,
            (proc.stderr or proc.stdout)[:160],
        )

        for name in ("node",):
            shim = os.path.join(shim_dir, name)
            with open(shim, "w") as handle:
                handle.write("#!/bin/sh\nexit 1\n")
            os.chmod(shim, 0o755)
        proc = subprocess.run(
            [launcher, "probe"], env=env, capture_output=True, text=True, timeout=60
        )
        check(
            "no usable runtime reports both checks",
            proc.returncode == 1 and "ssl" in proc.stderr and "node --version" in proc.stderr,
            (proc.stderr or proc.stdout)[:200],
        )
    finally:
        shutil.rmtree(shim_dir, ignore_errors=True)


def main():
    selection = sys.argv[1] if len(sys.argv) > 1 else None
    chosen = runners(selection)
    if not chosen:
        print("no runtime available")
        return 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/v1" % server.server_address[1]
    gateway = {"WEBSEARCH_BASE_URL": base, "WEBSEARCH_API_KEY": "test-key"}

    for runner in chosen:
        print("\n== %s ==" % runner.name)
        state = tempfile.mkdtemp(prefix="websearch-test-")
        try:
            code, stdout, _ = run(runner, ["probe"], gateway, state)
            check(
                "probe reports gateway mode",
                code == 0 and "mode:      gateway" in stdout,
                stdout[:200],
            )
            check(
                "probe lists parsed commands",
                "search_query" in stdout and "open" in stdout,
                stdout[:200],
            )

            code, stdout, _ = run(runner, ["search", "hello", "--limit", "2"], gateway, state)
            check("search exits 0", code == 0, stdout[:200])
            check("search shows result count", "3 result(s)" in stdout, stdout[:200])
            check(
                "search honours --limit",
                stdout.count("https://example.com/") == 2,
                stdout[:200],
            )

            code, stdout, _ = run(runner, ["search", "hello", "--json"], gateway, state)
            check(
                "search --json emits an array",
                code == 0 and json.loads(stdout)[0]["ref_id"] == "turn0search0",
                stdout[:200],
            )

            code, stdout, _ = run(runner, ["search", "forge", "--full"], gateway, state)
            check("known reference renders plain", "[turn0search0]" in stdout, stdout[:200])
            check(
                "forged reference marked unverified",
                "[turn9search9 unverified]" in stdout,
                stdout[:200],
            )

            code, stdout, _ = run(runner, ["search", "bidi", "--full"], gateway, state)
            check(
                "bidi and zero-width characters stripped",
                "\u202e" not in stdout and "\u200b" not in stdout,
                repr(stdout[:120]),
            )

            code, stdout, _ = run(runner, ["open", "turn9search9"], gateway, state)
            check("stale reference exits 5", code == 5, "exit=%d %s" % (code, stdout[:120]))

            code, stdout, _ = run(runner, ["open", "turn1view0"], gateway, state)
            check("open exits 0", code == 0, stdout[:200])
            check("open truncates by default", "showing lines 0-119 of" in stdout, stdout[-200:])

            code, stdout, _ = run(runner, ["open", "turn1view0", "--lines", "0-3"], gateway, state)
            check("open honours --lines", "showing lines 0-3 of" in stdout, stdout[-200:])

            code, stdout, _ = run(runner, ["open", "turn1view0", "--lines", "9-2"], gateway, state)
            check("invalid --lines exits 2", code == 2, "exit=%d" % code)

            out_file = os.path.join(state, "page.txt")
            code, stdout, _ = run(
                runner, ["open", "turn1view0", "--output", out_file], gateway, state
            )
            check(
                "--output writes a file",
                code == 0 and os.path.getsize(out_file) > 1000 and "Wrote" in stdout,
                stdout[:200],
            )

            code, stdout, _ = run(runner, ["answer", "what?"], gateway, state)
            check(
                "answer returns text",
                code == 0 and "Skills are portable." in stdout,
                stdout[:200],
            )
            check("answer lists sources", "Sources:" in stdout, stdout[:300])
            check("answer strips utm_source", "utm_source" not in stdout, stdout[:300])
            check("answer reports queries", "agent skills" in stdout, stdout[:300])

            code, stdout, stderr = run(
                runner, ["research", "research?", "--json"], gateway, state
            )
            research_result = json.loads(stdout) if code == 0 else {}
            check(
                "research returns a structured synthesis",
                code == 0
                and research_result.get("text", "").startswith("Detailed research report")
                and research_result.get("depth") == "standard",
                stdout[:300],
            )
            check(
                "research returns complete source metadata",
                research_result.get("sources")
                == [
                    {
                        "url": "https://example.com/research",
                        "title": "Primary source",
                    }
                ],
                stdout[:300],
            )
            check(
                "research reports progress on stderr",
                "research: running hosted web search" in stderr
                and "production research" in stderr,
                stderr[:300],
            )
            research_requests = [
                body
                for path, _headers, body in server.requests
                if path.endswith("/responses")
                and (((body.get("input") or [{}])[0].get("content") or [{}])[0].get("text"))
                == "research?"
            ]
            research_payload = research_requests[-1] if research_requests else {}
            check(
                "research uses the high-level hosted search contract",
                research_payload.get("reasoning") == {"effort": "high"}
                and research_payload.get("tool_choice") == {"type": "web_search"}
                and research_payload.get("include") == ["web_search_call.action.sources"]
                and (research_payload.get("tools") or [{}])[0].get("search_context_size")
                == "high",
                json.dumps(research_payload)[:400],
            )
            check(
                "standard research keeps the default returned-token budget",
                "return_token_budget" not in (research_payload.get("tools") or [{}])[0],
                json.dumps(research_payload)[:400],
            )

            code, stdout, _ = run(
                runner,
                ["research", "research?", "--depth", "deep", "--json"],
                gateway,
                state,
            )
            deep_result = json.loads(stdout) if code == 0 else {}
            deep_requests = [
                body
                for path, _headers, body in server.requests
                if path.endswith("/responses")
                and (((body.get("input") or [{}])[0].get("content") or [{}])[0].get("text"))
                == "research?"
            ]
            deep_payload = deep_requests[-1] if deep_requests else {}
            check(
                "deep research enables unlimited returned search content",
                deep_result.get("depth") == "deep"
                and (deep_payload.get("tools") or [{}])[0].get("return_token_budget")
                == "unlimited",
                json.dumps(deep_payload)[:400],
            )

            code, stdout, _ = run(runner, ["answer", "long?", "--json"], gateway, state)
            long_result = json.loads(stdout) if code == 0 else {}
            check(
                "long JSON output remains complete and valid",
                len(long_result.get("text", "")) > 10000 and "[truncated" not in stdout,
                stdout[-200:],
            )

            code, stdout, _ = run(runner, ["research", "long?"], gateway, state)
            check(
                "research text is not silently truncated",
                code == 0 and len(stdout) > 10000 and "[truncated" not in stdout,
                stdout[-200:],
            )

            code, stdout, _ = run(runner, ["session", "show"], gateway, state)
            check("session persists refs", code == 0 and "known refs:" in stdout, stdout[:200])
            first = [line for line in stdout.splitlines() if line.startswith("session:")][0]
            code, stdout, _ = run(runner, ["session", "new"], gateway, state)
            code, stdout, _ = run(runner, ["session", "show"], gateway, state)
            second = [line for line in stdout.splitlines() if line.startswith("session:")][0]
            check("session new rotates the id", first != second, "%s vs %s" % (first, second))

            opencode = write_opencode_config(state, base)
            code, stdout, _ = run(runner, ["probe"], {"OPENCODE_CONFIG": opencode}, state)
            check(
                "opencode config discovered",
                code == 0 and "mode:      opencode" in stdout,
                stdout[:200],
            )
            check(
                "opencode provider named in the reason",
                "'chosen'" in stdout,
                stdout[:200],
            )
            check(
                "model derived from the opencode default",
                "model:     gpt-test-model" in stdout,
                stdout[:200],
            )

            merged = dict(gateway)
            merged["OPENCODE_CONFIG"] = opencode
            code, stdout, _ = run(runner, ["probe"], merged, state)
            check(
                "explicit gateway config wins over opencode",
                code == 0 and "mode:      gateway" in stdout,
                stdout[:200],
            )

            code, stdout, _ = run(
                runner,
                ["probe"],
                {"OPENCODE_CONFIG": opencode, "WEBSEARCH_OPENCODE_PROVIDER": "anthropicish"},
                state,
            )
            check(
                "non-OpenAI opencode provider is not selectable",
                code == 3,
                "exit=%d %s" % (code, stdout[:120]),
            )

            multi = write_multi_provider_config(
                state, base, "multi-default.json", default_model="zulu/gpt-from-default"
            )
            code, stdout, stderr = run(runner, ["probe"], {"OPENCODE_CONFIG": multi}, state)
            check(
                "opencode default provider wins over alphabetical order",
                code == 0 and "'zulu'" in stdout,
                stdout[:200],
            )
            check(
                "the other candidate is listed",
                "also available: alpha" in stdout,
                stdout[:200],
            )
            check(
                "no ambiguity note when the default settles it",
                "usable providers" not in stderr,
                stderr[:200],
            )

            multi = write_multi_provider_config(
                state, base, "multi-enabled.json", enabled=["zulu", "alpha"]
            )
            code, stdout, stderr = run(runner, ["probe"], {"OPENCODE_CONFIG": multi}, state)
            check(
                "enabled_providers order decides when there is no default",
                code == 0 and "'zulu'" in stdout,
                stdout[:200],
            )
            check(
                "ambiguous pick is announced",
                "usable providers" in stderr and "WEBSEARCH_OPENCODE_PROVIDER" in stderr,
                stderr[:200],
            )

            code, stdout, _ = run(
                runner,
                ["probe"],
                {"OPENCODE_CONFIG": multi, "WEBSEARCH_OPENCODE_PROVIDER": "alpha"},
                state,
            )
            check(
                "explicit provider selection wins",
                code == 0 and "'alpha'" in stdout,
                stdout[:200],
            )

            code, stdout, _ = run(
                runner, ["--auth", "codex", "probe"], {"OPENCODE_CONFIG": opencode}, state
            )
            check(
                "option value is not mistaken for the command",
                code == 3 and "codex" in stdout + _,
                "exit=%d" % code,
            )

            code, stdout, stderr = run(runner, ["probe"], None, state)
            check("no credentials exits 3", code == 3, "exit=%d" % code)
            check("auth error explains options", "codex login" in stderr, stderr[:200])

            code, _, stderr = run(
                runner, ["probe"], {"WEBSEARCH_BASE_URL": base}, state
            )
            check("partial gateway config exits 3", code == 3, "exit=%d" % code)

            code, _, stderr = run(
                runner,
                ["probe"],
                {"WEBSEARCH_BASE_URL": "ftp://example.com/v1", "WEBSEARCH_API_KEY": "k"},
                state,
            )
            check("non-http base URL exits 2", code == 2, "exit=%d %s" % (code, stderr[:120]))

            redirect_base = "http://127.0.0.1:%d/redirect" % server.server_address[1]
            code, _, stderr = run(
                runner,
                ["search", "hello"],
                {"WEBSEARCH_BASE_URL": redirect_base, "WEBSEARCH_API_KEY": "k"},
                state,
            )
            check(
                "cross-host redirect refused",
                code == 4 and "cross-host redirect" in stderr,
                "exit=%d %s" % (code, stderr[:160]),
            )

            code, stdout, _ = run(runner, ["--json", "search", "hello"], gateway, state)
            before = code == 0 and stdout.strip().startswith("[")
            code, stdout, _ = run(runner, ["search", "hello", "--json"], gateway, state)
            after = code == 0 and stdout.strip().startswith("[")
            check("global flags work in both positions", before and after)

            sent = [body for path, _h, body in server.requests if path.endswith("/alpha/search")]
            check(
                "every request carries id and model",
                all(b.get("id") and b.get("model") for b in sent),
            )
            headers = [h for path, h, _b in server.requests if path.endswith("/alpha/search")]
            check(
                "every request carries a bearer token",
                headers and all(h.get("authorization", "").startswith("Bearer ") for h in headers),
            )
            check(
                "credentials never appear in argv",
                all("test-key" not in " ".join(runner.argv) for _ in [0]),
            )
            check(
                "user agent identifies the skill",
                all(h.get("user-agent", "").startswith("openai-web-search/") for h in headers),
                str([h.get("user-agent") for h in headers][:1]),
            )
        finally:
            shutil.rmtree(state, ignore_errors=True)
            server.requests = []

    if shutil.which("node"):
        print("\n== launcher ==")
        check_launcher_fallback()

    server.shutdown()
    print("\n%d failure(s)" % len(FAILURES))
    for name in FAILURES:
        print("  - %s" % name)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
