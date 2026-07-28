#!/usr/bin/env python3
"""websearch - live web search and page reading over OpenAI-compatible endpoints.

Reference implementation. scripts/websearch.mjs is a port with identical
successful output, exit codes, and session state; both are covered by the same
test suite. Standard library only.

Exit codes:
  0  success
  1  unexpected error
  2  usage error
  3  authentication unavailable or expired
  4  upstream HTTP error
  5  upstream returned HTTP 200 with an in-band failure
"""

import argparse
import base64
import errno
import hashlib
import json
import os
import re
import stat
import sys
import time
import unicodedata
import uuid
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit

try:  # Absent when the interpreter was built without OpenSSL headers.
    import ssl  # noqa: F401

    HTTPS_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the host interpreter
    HTTPS_AVAILABLE = False

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_UPSTREAM = 4
EXIT_CONTENT = 5

VERSION = "0.1.0"

CHATGPT_BACKEND = "https://chatgpt.com/backend-api/codex"
OPENAI_API_BASE = "https://api.openai.com/v1"

# The ChatGPT backend rejects default runtime user agents such as
# "Python-urllib/3.9" with HTTP 403. Identify honestly instead of
# impersonating another client.
USER_AGENT = "openai-web-search/%s" % VERSION

# Default models per auth mode. Override with WEBSEARCH_MODEL or --model;
# these are only starting points and change as providers ship new models.
DEFAULT_MODELS = {
    "codex-oauth": "gpt-5.6-sol",
    "gateway": "gpt-5.6-sol",
    "opencode": "gpt-5.6-sol",
    "openai-api": "gpt-5.6",
}

# OpenCode identifies each provider by its AI SDK package. Only OpenAI-shaped
# providers expose the Responses API this skill needs.
OPENCODE_OPENAI_PACKAGE = "@ai-sdk/openai"

CODEX_ORIGINATOR = "codex_cli_rs"

# Private Use Area delimiters that wrap citation and link markers in
# search output. See references/SECURITY.md for why these are stripped.
PUA_START = "\ue200"
PUA_END = "\ue201"
PUA_SEP = "\ue202"

MARKER_RE = re.compile(
    "%s([^%s%s]*)%s([^%s%s]*)%s"
    % (PUA_START, PUA_START, PUA_END, PUA_SEP, PUA_START, PUA_END, PUA_END)
)
REF_RE = re.compile(r"^turn\d+[a-z]+\d+$")
SCHEMA_COMMAND_RE = re.compile(r"^([a-z_]+)\?:", re.M)
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]{1,200})\]\((https?://[^\s)]+)\)")

# Bidirectional overrides and other invisible formatting characters are
# removed from upstream text: they let a page reorder what a reader sees.
INVISIBLE_CHARS = (
    "\u200b\u200c\u200d\u200e\u200f"  # zero width, LTR/RTL marks
    "\u202a\u202b\u202c\u202d\u202e"  # bidirectional embedding and override
    "\u2066\u2067\u2068\u2069"          # bidirectional isolates
    "\ufeff"                              # zero width no-break space
)

# Agent harnesses commonly truncate tool output past ~10-30K characters.
# Stay well under that unless the caller explicitly asks for more.
NARROW_NBSP = "\u202f"  # upstream timestamps use this; render it as a plain space

DEFAULT_STDOUT_BUDGET = 8000
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_SNIPPET_CHARS = 200
DEFAULT_PAGE_LINES = 120
DEFAULT_TIMEOUT = 120
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

# Passes API-level validation but fails the upstream tool parser, which
# answers with its own schema. Costs no web egress. See references/COMMANDS.md.
PROBE_COMMANDS = {"sports": [{"fn": "standings", "league": "nba"}]}


class CliError(Exception):
    def __init__(self, message, code=EXIT_ERROR, hint=None):
        Exception.__init__(self, message)
        self.message = message
        self.code = code
        self.hint = hint


def eprint(*parts):
    sys.stderr.write(" ".join(str(p) for p in parts) + "\n")


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def jwt_claims(token):
    """Decode a JWT payload without verifying it. Used only to read `exp`."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def codex_home():
    override = os.environ.get("CODEX_HOME", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".codex")


def load_codex_auth():
    """Return (tokens, problem). `problem` is None when the session is usable."""
    path = os.path.join(codex_home(), "auth.json")
    if not os.path.exists(path):
        return None, "no Codex credentials at %s" % path
    try:
        mode = os.stat(path).st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            eprint("warning: %s is readable beyond its owner; run: chmod 600 %s" % (path, path))
        with open(path, "r") as handle:
            data = json.load(handle)
    except (IOError, OSError) as exc:
        return None, "cannot read %s: %s" % (path, exc)
    except ValueError as exc:
        return None, "%s is not valid JSON: %s" % (path, exc)

    tokens = data.get("tokens") or {}
    access = (tokens.get("access_token") or "").strip()
    account = (tokens.get("account_id") or "").strip()
    if not access:
        return None, "%s has no access token" % path
    expires = jwt_claims(access).get("exp")
    if expires and float(expires) <= time.time() + 60:
        return None, "the Codex access token in %s has expired" % path
    return {"access_token": access, "account_id": account, "expires": expires}, None


def opencode_config_path():
    override = os.environ.get("OPENCODE_CONFIG", "").strip()
    if override:
        return override
    root = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if not root:
        root = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(root, "opencode", "opencode.json")


def load_opencode_provider():
    """Return (provider, problem) for an OpenAI-shaped OpenCode provider.

    OpenCode stores a base URL and API key per provider. Reusing that config
    means a machine already set up for OpenCode needs no extra configuration.
    """
    path = opencode_config_path()
    if not os.path.exists(path):
        return None, "no OpenCode config at %s" % path
    try:
        mode = os.stat(path).st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            eprint(
                "warning: %s contains an API key and is readable beyond its owner; "
                "run: chmod 600 %s" % (path, path)
            )
        with open(path, "r") as handle:
            data = json.load(handle)
    except (IOError, OSError) as exc:
        return None, "cannot read %s: %s" % (path, exc)
    except ValueError as exc:
        return None, "%s is not valid JSON: %s" % (path, exc)

    providers = data.get("provider") or {}
    candidates = []
    for name in sorted(providers):
        config = providers.get(name) or {}
        if config.get("npm") != OPENCODE_OPENAI_PACKAGE:
            continue
        options = config.get("options") or {}
        base = (options.get("baseURL") or options.get("baseUrl") or "").strip()
        key = (options.get("apiKey") or "").strip()
        if base and key:
            candidates.append({"name": name, "base": base, "key": key})

    wanted = os.environ.get("WEBSEARCH_OPENCODE_PROVIDER", "").strip()
    if wanted:
        for candidate in candidates:
            if candidate["name"] == wanted:
                candidates = [candidate]
                break
        else:
            return None, "%s has no %s provider named %r" % (
                path,
                OPENCODE_OPENAI_PACKAGE,
                wanted,
            )
    if not candidates:
        return None, "%s has no %s provider with both baseURL and apiKey" % (
            path,
            OPENCODE_OPENAI_PACKAGE,
        )

    # The top-level default is written as "<provider>/<model>". When several
    # providers qualify, prefer the one OpenCode itself defaults to, then the
    # order given in enabled_providers, then the name. Alphabetical order alone
    # would pick an arbitrary endpoint.
    default_model = str(data.get("model") or "")
    default_provider = default_model.split("/", 1)[0] if "/" in default_model else ""
    enabled = [name for name in (data.get("enabled_providers") or []) if isinstance(name, str)]

    def rank(candidate):
        name = candidate["name"]
        if name == default_provider:
            return (0, 0, name)
        if name in enabled:
            return (1, enabled.index(name), name)
        return (2, 0, name)

    candidates.sort(key=rank)
    chosen = candidates[0]
    chosen["alternatives"] = [candidate["name"] for candidate in candidates[1:]]
    # Only speak up when the pick was not settled by OpenCode's own default:
    # otherwise the choice is unambiguous and a note on every call is noise.
    if chosen["alternatives"] and not wanted and chosen["name"] != default_provider:
        eprint(
            "note: %s has %d usable providers (%s); using %r. "
            "Set WEBSEARCH_OPENCODE_PROVIDER to choose another."
            % (
                path,
                len(candidates),
                ", ".join(candidate["name"] for candidate in candidates),
                chosen["name"],
            )
        )
    prefix = chosen["name"] + "/"
    if default_model.startswith(prefix):
        chosen["model"] = default_model[len(prefix) :]
    return chosen, None


class Endpoint(object):
    def __init__(self, mode, headers, responses_url, search_url, model, reason):
        self.mode = mode
        self.headers = headers
        self.responses_url = responses_url
        self.search_url = search_url
        self.model = model
        self.reason = reason

    @property
    def supports_codex_layer(self):
        return self.search_url is not None

    @property
    def identity(self):
        seed = "%s|%s" % (self.mode, self.responses_url or self.search_url or "")
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def normalize_base(raw):
    base = raw.strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https"):
        raise CliError(
            "WEBSEARCH_BASE_URL must start with http:// or https://; got %r" % raw,
            EXIT_USAGE,
        )
    if not parts.netloc:
        raise CliError("WEBSEARCH_BASE_URL has no host: %r" % raw, EXIT_USAGE)
    return base


def resolve_endpoint(model_override=None, force_mode=None):
    """Pick an endpoint. Explicit gateway config wins, then Codex OAuth, then an API key."""
    wanted = (force_mode or os.environ.get("WEBSEARCH_AUTH", "")).strip().lower()
    base = os.environ.get("WEBSEARCH_BASE_URL", "").strip()
    gateway_key = os.environ.get("WEBSEARCH_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model_env = os.environ.get("WEBSEARCH_MODEL", "").strip()

    def pick_model(mode):
        return model_override or model_env or DEFAULT_MODELS[mode]

    attempts = []

    if wanted in ("", "gateway") and base and gateway_key:
        root = normalize_base(base)
        return Endpoint(
            mode="gateway",
            headers={
                "Authorization": "Bearer " + gateway_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Originator": CODEX_ORIGINATOR,
            },
            responses_url=root + "/responses",
            search_url=root + "/alpha/search",
            model=pick_model("gateway"),
            reason="WEBSEARCH_BASE_URL and WEBSEARCH_API_KEY are set",
        )
    if wanted == "gateway":
        raise CliError(
            "WEBSEARCH_AUTH=gateway requires both WEBSEARCH_BASE_URL and WEBSEARCH_API_KEY",
            EXIT_AUTH,
        )
    if base and not gateway_key:
        attempts.append("WEBSEARCH_BASE_URL is set but WEBSEARCH_API_KEY is not")
    elif gateway_key and not base:
        attempts.append("WEBSEARCH_API_KEY is set but WEBSEARCH_BASE_URL is not")

    if wanted in ("", "opencode"):
        provider, problem = load_opencode_provider()
        if provider:
            root = normalize_base(provider["base"])
            model = model_override or model_env or provider.get("model")
            reason = "using the OpenCode provider %r in %s" % (
                provider["name"],
                opencode_config_path(),
            )
            if provider.get("alternatives"):
                reason += " (also available: %s)" % ", ".join(provider["alternatives"])
            return Endpoint(
                mode="opencode",
                headers={
                    "Authorization": "Bearer " + provider["key"],
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Originator": CODEX_ORIGINATOR,
                },
                responses_url=root + "/responses",
                search_url=root + "/alpha/search",
                model=model or DEFAULT_MODELS["opencode"],
                reason=reason,
            )
        attempts.append(problem)
        if wanted == "opencode":
            raise CliError(
                problem,
                EXIT_AUTH,
                hint=(
                    "Add a provider with \"npm\": \"%s\" and options.baseURL plus "
                    "options.apiKey to your OpenCode config." % OPENCODE_OPENAI_PACKAGE
                ),
            )

    if wanted in ("", "codex"):
        tokens, problem = load_codex_auth()
        if tokens:
            headers = {
                "Authorization": "Bearer " + tokens["access_token"],
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Originator": CODEX_ORIGINATOR,
            }
            if tokens["account_id"]:
                headers["Chatgpt-Account-Id"] = tokens["account_id"]
            return Endpoint(
                mode="codex-oauth",
                headers=headers,
                responses_url=CHATGPT_BACKEND + "/responses",
                search_url=CHATGPT_BACKEND + "/alpha/search",
                model=pick_model("codex-oauth"),
                reason="using the Codex CLI session in %s" % codex_home(),
            )
        attempts.append(problem)
        if wanted == "codex":
            raise CliError(problem, EXIT_AUTH, hint="Run `codex login` to refresh the session.")

    if wanted in ("", "openai") and openai_key:
        return Endpoint(
            mode="openai-api",
            headers={
                "Authorization": "Bearer " + openai_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            responses_url=OPENAI_API_BASE + "/responses",
            search_url=None,
            model=pick_model("openai-api"),
            reason="OPENAI_API_KEY is set",
        )
    if wanted == "openai":
        raise CliError("WEBSEARCH_AUTH=openai requires OPENAI_API_KEY", EXIT_AUTH)
    if wanted not in ("", "gateway", "opencode", "codex", "openai"):
        raise CliError(
            "WEBSEARCH_AUTH must be one of: gateway, opencode, codex, openai; got %r" % wanted,
            EXIT_USAGE,
        )

    attempts.append("OPENAI_API_KEY is not set")
    raise CliError(
        "no usable credentials found (%s)" % "; ".join(a for a in attempts if a),
        EXIT_AUTH,
        hint=(
            "Configure one of, in the order they are tried:\n"
            "  1. A gateway      - export WEBSEARCH_BASE_URL and WEBSEARCH_API_KEY\n"
            "  2. OpenCode       - a provider with \"npm\": \"%s\" in your OpenCode config\n"
            "  3. A Codex session - run `codex login` (no API key needed)\n"
            "  4. The OpenAI API - export OPENAI_API_KEY (hosted search only)"
            % OPENCODE_OPENAI_PACKAGE
        ),
    )


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class SameHostRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Refuse redirects that would send credentials to a different host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            raise CliError(
                "refusing to follow a cross-host redirect from %s to %s"
                % (urlsplit(req.full_url).netloc, urlsplit(newurl).netloc),
                EXIT_UPSTREAM,
            )
        return urlrequest.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl
        )


_OPENER = urlrequest.build_opener(SameHostRedirectHandler)


def require_https_support(url):
    """Fail early and clearly on an interpreter built without ssl.

    urllib reports that case as "unknown url type: https", which points at the
    URL rather than at the interpreter.
    """
    if not url.lower().startswith("https:") or HTTPS_AVAILABLE:
        return
    raise CliError(
        "this Python cannot make https requests: it was built without the ssl module",
        EXIT_ERROR,
        hint=(
            "Verify with: python3 -c 'import ssl'\n"
            "Then either install a Python with ssl support, or run the Node "
            "implementation instead: WEBSEARCH_RUNTIME=node"
        ),
    )


def post(url, headers, payload, timeout, stream=False):
    """POST JSON. Returns (status, text). Never logs credentials."""
    require_https_support(url)
    body = json.dumps(payload).encode("utf-8")
    request = urlrequest.Request(url, data=body, method="POST")
    request.add_header("User-Agent", USER_AGENT)
    for key, value in headers.items():
        request.add_header(key, value)
    if stream:
        request.add_header("Accept", "text/event-stream")
    try:
        response = _OPENER.open(request, timeout=timeout)
    except urlerror.HTTPError as exc:
        detail = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
        return exc.code, detail
    except urlerror.URLError as exc:
        raise CliError("cannot reach %s: %s" % (url, exc.reason), EXIT_UPSTREAM) from exc
    except OSError as exc:
        if exc.errno == errno.ETIMEDOUT:
            raise CliError("timed out contacting %s" % url, EXIT_UPSTREAM) from exc
        raise CliError("cannot reach %s: %s" % (url, exc), EXIT_UPSTREAM) from exc
    with response:
        return response.getcode(), response.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")


def upstream_failure(url, status, text):
    message = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                message = err.get("message") or json.dumps(err)
            elif isinstance(err, str):
                message = err
            elif parsed.get("detail"):
                message = str(parsed["detail"])
    except ValueError:
        pass
    hint = None
    if status in (401, 403):
        hint = (
            "Credentials were rejected. For a Codex session run `codex login`; "
            "for a gateway check WEBSEARCH_API_KEY."
        )
    elif status == 404:
        hint = "The endpoint does not expose this route. Run `websearch probe`."
    elif status == 429:
        hint = "Rate limited upstream. Retry later or reduce request volume."
    return CliError(
        "%s returned HTTP %d: %s" % (urlsplit(url).netloc, status, message[:500]),
        EXIT_UPSTREAM,
        hint=hint,
    )


def parse_sse(text):
    """Collect JSON payloads from an SSE stream."""
    events = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            events.append(json.loads(chunk))
        except ValueError:
            continue
    return events


# --------------------------------------------------------------------------
# Sanitization
# --------------------------------------------------------------------------


def strip_invisible(text):
    out = []
    for char in text:
        if char in INVISIBLE_CHARS:
            continue
        code = ord(char)
        if 0xE000 <= code <= 0xF8FF:  # Private Use Area
            continue
        if unicodedata.category(char) == "Cc" and char not in "\t\n\r":
            continue
        if char == NARROW_NBSP:
            out.append(" ")
            continue
        out.append(char)
    return "".join(out)


def sanitize(text, known_refs=None):
    """Render upstream markers as plain text and remove spoofable characters.

    Citation markers carry reference ids the caller needs for follow-up
    commands, so they are rendered rather than dropped. A reference that is
    not corroborated by the structured `results` array is marked unverified:
    page content can contain the same delimiters and forge one.
    """
    if not text:
        return ""
    refs = known_refs or set()

    def render(match):
        kind = match.group(1)
        payload = match.group(2)
        if kind != "cite":
            return "[%s %s]" % (strip_invisible(kind), strip_invisible(payload))
        if REF_RE.match(payload):
            return "[%s]" % payload if payload in refs else "[%s unverified]" % payload
        if "†" in payload:
            fields = payload.split("†")
            link_id = fields[0].strip()
            label = strip_invisible(fields[1]).strip() if len(fields) > 1 else ""
            domain = strip_invisible(fields[2]).strip() if len(fields) > 2 else ""
            label = re.sub(r"\s+", " ", label)[:80]
            if domain:
                return "{link %s: %s -> %s}" % (link_id, label, domain)
            return "{link %s: %s}" % (link_id, label)
        return "[%s]" % strip_invisible(payload)

    return strip_invisible(MARKER_RE.sub(render, text))


TRACKING_PATTERNS = (
    ("?utm_source=openai&", "?"),
    ("&utm_source=openai", ""),
    ("?utm_source=openai", ""),
)


def strip_tracking(text):
    """Drop the tracking parameter upstream appends to cited URLs.

    Applied to bare URLs and to answer text alike, so an inline markdown
    citation the agent copies forward stays canonical.
    """
    if not text:
        return text
    for old, new in TRACKING_PATTERNS:
        text = text.replace(old, new)
    return text


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------


def state_path():
    root = os.environ.get("XDG_STATE_HOME", "").strip()
    if not root:
        root = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(root, "openai-web-search", "state.json")


def load_state():
    try:
        with open(state_path(), "r") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (IOError, OSError, ValueError):
        return {}


def save_state(data):
    path = state_path()
    directory = os.path.dirname(path)
    try:
        if not os.path.isdir(directory):
            os.makedirs(directory, 0o700)
        temporary = path + ".tmp"
        with open(temporary, "w") as handle:
            json.dump(data, handle)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except (IOError, OSError) as exc:
        eprint("warning: cannot persist session state: %s" % exc)


def get_session(endpoint, reset=False):
    state = load_state()
    sessions = state.setdefault("sessions", {})
    entry = sessions.get(endpoint.identity)
    if reset or not isinstance(entry, dict) or not entry.get("id"):
        entry = {"id": str(uuid.uuid4()), "created": int(time.time()), "refs": {}}
        sessions[endpoint.identity] = entry
        save_state(state)
    return entry


def remember_refs(endpoint, results):
    if not results:
        return
    state = load_state()
    sessions = state.setdefault("sessions", {})
    entry = sessions.get(endpoint.identity)
    if not isinstance(entry, dict):
        return
    refs = entry.setdefault("refs", {})
    for item in results:
        ref = item.get("ref_id")
        if not ref:
            continue
        refs[ref] = {
            "url": strip_tracking(item.get("url") or ""),
            "title": (item.get("title") or "")[:200],
        }
    # Keep the map bounded; only recent references stay addressable upstream.
    if len(refs) > 400:
        for key in list(refs)[: len(refs) - 400]:
            del refs[key]
    save_state(state)


def known_refs(endpoint):
    entry = load_state().get("sessions", {}).get(endpoint.identity)
    if isinstance(entry, dict):
        return set(entry.get("refs", {}))
    return set()


# --------------------------------------------------------------------------
# Codex search layer
# --------------------------------------------------------------------------


def detect_inband_error(data):
    """Upstream reports several failures with HTTP 200. Find them."""
    output = data.get("output") or ""
    head = output[:400]
    if head.startswith("Error parsing function call"):
        return "upstream rejected the command payload: " + head.split("\n")[0]
    if "Unable to resolve open call" in head or "invalid ref_id" in head:
        return (
            "a reference id in this request is not valid for the current session. "
            "Reference ids are scoped to one session and expire; re-run the search, "
            "or pass a full URL instead"
        )
    if head.startswith("Internal Error"):
        return "upstream returned an internal error: " + head.split("\n")[0]
    if head.startswith("Found no tool response"):
        return "upstream could not interpret the command arguments: " + head.split("\n")[0]
    for item in data.get("results") or []:
        if item.get("title") == "Internal Error" and not item.get("url"):
            return "upstream returned an internal error: %s" % (item.get("snippet") or "")[:200]
    return None


def call_search(endpoint, commands, timeout, session=None, settings=None):
    if not endpoint.supports_codex_layer:
        raise CliError(
            "this endpoint does not expose the Codex search layer (mode: %s)" % endpoint.mode,
            EXIT_USAGE,
            hint="Use `websearch answer` instead, or configure a Codex session or gateway.",
        )
    entry = session or get_session(endpoint)
    payload = {"id": entry["id"], "model": endpoint.model, "commands": commands}
    if settings:
        payload["settings"] = settings
    status, text = post(endpoint.search_url, endpoint.headers, payload, timeout)
    if status != 200:
        raise upstream_failure(endpoint.search_url, status, text)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CliError(
            "upstream returned a non-JSON search response", EXIT_UPSTREAM
        ) from exc
    remember_refs(endpoint, data.get("results") or [])
    problem = detect_inband_error(data)
    if problem:
        raise CliError(problem, EXIT_CONTENT)
    return data


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def emit(text, args, kind="output"):
    """Write text to stdout, an explicit file, or a truncated preview."""
    if getattr(args, "output", None):
        with open(args.output, "w") as handle:
            handle.write(text)
        print("Wrote %d characters of %s to %s" % (len(text), kind, args.output))
        return
    if getattr(args, "full", False) or len(text) <= DEFAULT_STDOUT_BUDGET:
        print(text)
        return
    print(text[:DEFAULT_STDOUT_BUDGET])
    print(
        "\n[truncated %d of %d characters. Re-run with --full, or with "
        "--output FILE to capture everything.]"
        % (len(text) - DEFAULT_STDOUT_BUDGET, len(text))
    )


def render_results(data, endpoint, limit, snippet_chars):
    results = data.get("results") or []
    refs = known_refs(endpoint)
    lines = []
    for item in results[:limit]:
        ref = item.get("ref_id") or "-"
        title = sanitize(item.get("title") or "(untitled)", refs)
        url = strip_tracking(item.get("url") or "")
        snippet = re.sub(r"\s+", " ", sanitize(item.get("snippet") or "", refs)).strip()
        lines.append("%s  %s" % (ref, title))
        if url:
            lines.append("    %s" % url)
        if snippet:
            lines.append("    %s" % snippet[:snippet_chars])
        lines.append("")
    if not results:
        return sanitize(data.get("output") or "", refs)
    header = "%d result(s)" % len(results)
    if len(results) > limit:
        header += " (showing %d; --limit raises this)" % limit
    return header + "\n\n" + "\n".join(lines).rstrip()


def slice_lines(text, spec, default_lines):
    lines = text.split("\n")
    if not spec:
        if len(lines) <= default_lines:
            return text, None
        shown = "\n".join(lines[:default_lines])
        return shown, "showing lines 0-%d of %d; use --lines A-B or --full" % (
            default_lines - 1,
            len(lines),
        )
    match = re.match(r"^(\d+)-(\d+)$", spec.strip())
    if not match:
        raise CliError("--lines expects A-B, for example 0-120; got %r" % spec, EXIT_USAGE)
    start, end = int(match.group(1)), int(match.group(2))
    if start > end:
        raise CliError("--lines start must not exceed end; got %r" % spec, EXIT_USAGE)
    return "\n".join(lines[start : end + 1]), "showing lines %d-%d of %d" % (
        start,
        end,
        len(lines),
    )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_probe(args, endpoint):
    print("mode:      %s" % endpoint.mode)
    print("reason:    %s" % endpoint.reason)
    print("model:     %s" % endpoint.model)
    if endpoint.responses_url:
        print("responses: %s" % endpoint.responses_url)
    print("search:    %s" % (endpoint.search_url or "not available in this mode"))
    print("hosted layer:  available (websearch answer)")
    if not endpoint.supports_codex_layer:
        print("search layer:  unavailable")
        print("\nThis endpoint offers hosted web search only.")
        return EXIT_OK

    entry = get_session(endpoint)
    print("session:   %s" % entry["id"])
    payload = {"id": entry["id"], "model": endpoint.model, "commands": PROBE_COMMANDS}
    status, text = post(endpoint.search_url, endpoint.headers, payload, args.timeout)
    if status != 200:
        print("search layer:  rejected the probe (HTTP %d)" % status)
        raise upstream_failure(endpoint.search_url, status, text)
    try:
        output = (json.loads(text) or {}).get("output") or ""
    except ValueError:
        output = ""
    tail = output[output.find("Expected: type run") :] if output else ""
    commands = SCHEMA_COMMAND_RE.findall(tail)
    if commands:
        print("search layer:  available")
        print("commands:  %s" % ", ".join(sorted(commands)))
    else:
        print("search layer:  responded, but did not report a command schema")
        print("           the endpoint is reachable; treat the command set as unverified")
    return EXIT_OK


def cmd_search(args, endpoint):
    query = {"q": args.query}
    if args.recency is not None:
        query["recency"] = args.recency
    if args.domain:
        query["domains"] = args.domain
    commands = {"search_query": [query], "response_length": args.length}
    settings = {"external_web_access": False} if args.cached else None
    data = call_search(endpoint, commands, args.timeout, settings=settings)
    if args.json:
        emit(json.dumps(data.get("results") or [], indent=2), args, "JSON results")
        return EXIT_OK
    if args.full or args.output:
        emit(sanitize(data.get("output") or "", known_refs(endpoint)), args)
        return EXIT_OK
    emit(render_results(data, endpoint, args.limit, args.snippet), args)
    return EXIT_OK


def cmd_open(args, endpoint):
    operation = {"ref_id": args.ref}
    if args.lineno is not None:
        operation["lineno"] = args.lineno
    data = call_search(
        endpoint, {"open": [operation], "response_length": args.length}, args.timeout
    )
    return emit_page(data, args, endpoint)


def cmd_find(args, endpoint):
    data = call_search(
        endpoint,
        {"find": [{"ref_id": args.ref, "pattern": args.pattern}], "response_length": args.length},
        args.timeout,
    )
    return emit_page(data, args, endpoint)


def cmd_click(args, endpoint):
    data = call_search(
        endpoint,
        {"click": [{"ref_id": args.ref, "id": args.link_id}], "response_length": args.length},
        args.timeout,
    )
    return emit_page(data, args, endpoint)


def emit_page(data, args, endpoint):
    text = sanitize(data.get("output") or "", known_refs(endpoint))
    if args.json:
        emit(json.dumps({"output": text, "results": data.get("results") or []}, indent=2), args)
        return EXIT_OK
    if args.full or args.output:
        emit(text, args)
        return EXIT_OK
    shown, note = slice_lines(text, args.lines, DEFAULT_PAGE_LINES)
    emit(shown, args)
    if note:
        print("\n[%s]" % note)
    return EXIT_OK


def cmd_raw(args, endpoint):
    commands = read_json_argument(args.commands)
    if not isinstance(commands, dict):
        raise CliError("raw expects a JSON object of commands", EXIT_USAGE)
    data = call_search(endpoint, commands, args.timeout)
    if args.json:
        emit(json.dumps(data, indent=2), args)
        return EXIT_OK
    emit(sanitize(data.get("output") or "", known_refs(endpoint)), args)
    return EXIT_OK


def read_json_argument(value):
    if value == "-":
        raw = sys.stdin.read()
    elif value.startswith("@"):
        try:
            with open(value[1:], "r") as handle:
                raw = handle.read()
        except (IOError, OSError) as exc:
            raise CliError("cannot read %s: %s" % (value[1:], exc), EXIT_USAGE) from exc
    else:
        raw = value
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise CliError("invalid JSON: %s" % exc, EXIT_USAGE) from exc


def cmd_answer(args, endpoint):
    tool = {"type": "web_search", "external_web_access": not args.cached}
    if args.context:
        tool["search_context_size"] = args.context
    filters = {}
    if args.domain:
        filters["allowed_domains"] = args.domain
    if args.block:
        filters["blocked_domains"] = args.block
    if filters:
        tool["filters"] = filters

    payload = {
        "model": endpoint.model,
        "stream": True,  # the ChatGPT backend rejects stream:false
        "store": False,
        "instructions": (
            "Answer using live web search. Cite each claim with a markdown link "
            "to the page that supports it."
        ),
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": args.question}],
            }
        ],
        "tools": [tool],
    }
    status, text = post(
        endpoint.responses_url, endpoint.headers, payload, args.timeout, stream=True
    )
    if status != 200:
        raise upstream_failure(endpoint.responses_url, status, text)

    answer, citations, searches = collect_answer(parse_sse(text))
    if args.json:
        emit(
            json.dumps({"text": answer, "citations": citations, "searches": searches}, indent=2),
            args,
        )
        return EXIT_OK
    if not answer:
        raise CliError(
            "the endpoint completed the stream without producing answer text",
            EXIT_CONTENT,
            hint=(
                "This is usually transient; retry once. Use --json to inspect the "
                "raw stream if it persists."
            ),
        )
    body = [strip_tracking(strip_invisible(answer))]
    if searches:
        body.append("\nQueries run: %s" % "; ".join(searches))
    if citations:
        body.append("\nSources:")
        for item in citations:
            body.append("  - %s %s" % (item["title"] or "(untitled)", item["url"]))
    emit("\n".join(body), args)
    return EXIT_OK


def collect_answer(events):
    """Aggregate an SSE run into answer text, citations and executed queries.

    Citations arrive inconsistently: some backends emit annotation events,
    some attach annotations to the finished content part, and some only
    produce the markdown links the instructions asked for. All three are
    read, and the markdown links act as the final fallback.
    """
    answer = ""
    deltas = []
    citations = []
    searches = []
    seen = set()

    def add_citation(note):
        if not isinstance(note, dict) or note.get("type") != "url_citation":
            return
        url = strip_tracking(note.get("url") or "")
        if url and url not in seen:
            seen.add(url)
            citations.append({"url": url, "title": note.get("title") or ""})

    for event in events:
        kind = event.get("type")
        if kind == "response.output_text.delta":
            deltas.append(event.get("delta") or "")
        elif kind == "response.output_text.done":
            answer = event.get("text") or answer
        elif kind == "response.output_text.annotation.added":
            add_citation(event.get("annotation") or {})
        elif kind == "response.content_part.done":
            part = event.get("part") if isinstance(event.get("part"), dict) else event
            if part.get("text"):
                answer = part["text"]
            for note in part.get("annotations") or []:
                add_citation(note)
        elif kind == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "web_search_call":
                action = item.get("action") or {}
                action_queries = action.get("queries") or (
                    [action["query"]] if action.get("query") else []
                )
                for query in action_queries:
                    if query not in searches:
                        searches.append(query)
        elif kind == "response.completed":
            for item in (event.get("response") or {}).get("output") or []:
                if item.get("type") != "message":
                    continue
                for part in item.get("content") or []:
                    if part.get("text"):
                        answer = part["text"]
                    for note in part.get("annotations") or []:
                        add_citation(note)

    text = answer or "".join(deltas)
    if not citations:
        for title, url in MARKDOWN_LINK_RE.findall(text):
            add_citation({"type": "url_citation", "url": url, "title": title})
    return text, citations, searches


def cmd_session(args, endpoint):
    if args.action == "new":
        entry = get_session(endpoint, reset=True)
        print("new session: %s" % entry["id"])
        return EXIT_OK
    entry = get_session(endpoint)
    age = int(time.time()) - int(entry.get("created") or 0)
    print("session:   %s" % entry["id"])
    print("endpoint:  %s (%s)" % (endpoint.mode, endpoint.identity))
    print("age:       %d minute(s)" % (age // 60))
    print("known refs: %d" % len(entry.get("refs") or {}))
    for ref, meta in list((entry.get("refs") or {}).items())[-15:]:
        print("  %s  %s" % (ref, meta.get("url") or meta.get("title") or ""))
    return EXIT_OK


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


GLOBAL_DEFAULTS = {
    "model": None,
    "auth": None,
    "timeout": DEFAULT_TIMEOUT,
    "json": False,
    "full": False,
    "output": None,
    "lines": None,
    "length": "short",
}


def build_common():
    """Global options, accepted both before and after the subcommand.

    argparse only honours options on the parser that defines them, so the same
    options are attached to every subparser. SUPPRESS keeps an unset option
    from overwriting one that was given earlier on the command line.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default=argparse.SUPPRESS, help="override the model id")
    common.add_argument(
        "--auth",
        choices=["gateway", "opencode", "codex", "openai"],
        default=argparse.SUPPRESS,
        help="force an authentication mode",
    )
    common.add_argument(
        "--timeout",
        type=int,
        default=argparse.SUPPRESS,
        help="seconds to wait (default: %d)" % DEFAULT_TIMEOUT,
    )
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="emit structured JSON"
    )
    common.add_argument(
        "--full", action="store_true", default=argparse.SUPPRESS, help="do not truncate output"
    )
    common.add_argument(
        "--output", metavar="FILE", default=argparse.SUPPRESS, help="write full output to FILE"
    )
    return common


def build_parser():
    common = build_common()
    parser = argparse.ArgumentParser(
        prog="websearch",
        parents=[common],
        description=(
            "Search the live web, open pages, and follow links through a Codex "
            "session or any OpenAI-compatible endpoint."
        ),
        epilog=(
            "Credentials are discovered in this order and never read from arguments:\n"
            "  1. WEBSEARCH_BASE_URL + WEBSEARCH_API_KEY  an OpenAI-compatible gateway\n"
            "  2. OPENCODE_CONFIG (default ~/.config/opencode/opencode.json)\n"
            "  3. CODEX_HOME (default ~/.codex)           a Codex CLI session\n"
            "  4. OPENAI_API_KEY                          the OpenAI API, hosted search only\n"
            "  WEBSEARCH_MODEL, WEBSEARCH_AUTH, WEBSEARCH_OPENCODE_PROVIDER  overrides\n\n"
            "Exit codes: 0 ok, 1 error, 2 usage, 3 auth, 4 upstream, 5 in-band failure."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    probe = subparsers.add_parser(
        "probe", parents=[common], help="report the active mode and supported commands"
    )
    probe.set_defaults(handler=cmd_probe)

    answer = subparsers.add_parser(
        "answer",
        parents=[common],
        help="ask a question and get a cited answer (works on every mode)",
    )
    answer.add_argument("question")
    answer.add_argument("--domain", action="append", help="restrict to a domain (repeatable)")
    answer.add_argument("--block", action="append", help="exclude a domain (repeatable)")
    answer.add_argument(
        "--context", choices=["low", "medium", "high"], help="search context size"
    )
    answer.add_argument("--cached", action="store_true", help="use cached results, no live fetch")
    answer.set_defaults(handler=cmd_answer)

    search = subparsers.add_parser(
        "search", parents=[common], help="run a web search and list the results"
    )
    search.add_argument("query")
    search.add_argument(
        "--recency", type=int, metavar="DAYS", help="only results from the last DAYS"
    )
    search.add_argument("--domain", action="append", help="restrict to a domain (repeatable)")
    search.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_SEARCH_LIMIT,
        help="results to show (default: %(default)s)",
    )
    search.add_argument(
        "--snippet",
        type=int,
        default=DEFAULT_SNIPPET_CHARS,
        help="snippet characters (default: %(default)s)",
    )
    search.add_argument("--cached", action="store_true", help="use the cached index, no live fetch")
    add_length(search)
    search.set_defaults(handler=cmd_search)

    opener = subparsers.add_parser(
        "open", parents=[common], help="open a page by reference id or URL"
    )
    opener.add_argument("ref", help="a reference id such as turn1search0, or a full URL")
    opener.add_argument("--lineno", type=int, help="position the viewport at this line")
    add_lines(opener)
    add_length(opener)
    opener.set_defaults(handler=cmd_open)

    finder = subparsers.add_parser(
        "find", parents=[common], help="find a pattern inside an opened page"
    )
    finder.add_argument("ref")
    finder.add_argument("pattern")
    add_lines(finder)
    add_length(finder)
    finder.set_defaults(handler=cmd_find)

    clicker = subparsers.add_parser(
        "click", parents=[common], help="follow a numbered link from an opened page"
    )
    clicker.add_argument("ref")
    clicker.add_argument("link_id", type=int, metavar="ID", help="link id shown as {link ID: ...}")
    add_lines(clicker)
    add_length(clicker)
    clicker.set_defaults(handler=cmd_click)

    raw = subparsers.add_parser(
        "raw",
        parents=[common],
        help="send a full command object (JSON string, @file, or - for stdin)",
    )
    raw.add_argument("commands")
    raw.set_defaults(handler=cmd_raw)

    session = subparsers.add_parser(
        "session", parents=[common], help="inspect or rotate the reference scope"
    )
    session.add_argument("action", choices=["show", "new"], nargs="?", default="show")
    session.set_defaults(handler=cmd_session)

    return parser


def add_length(sub):
    sub.add_argument(
        "--length",
        choices=["short", "medium", "long"],
        default="short",
        help="upstream response length (default: %(default)s)",
    )


def add_lines(sub):
    sub.add_argument("--lines", metavar="A-B", help="show only this line range")


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE
    for name, fallback in GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, fallback)
    endpoint = resolve_endpoint(model_override=args.model, force_mode=args.auth)
    return args.handler(args, endpoint)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except CliError as error:
        sys.stdout.flush()  # keep partial results ahead of the diagnostic
        eprint("Error: %s" % error.message)
        if error.hint:
            eprint(error.hint)
        sys.exit(error.code)
    except KeyboardInterrupt:
        sys.exit(130)
