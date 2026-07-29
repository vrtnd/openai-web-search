# openai-web-search

[![skills.sh](https://skills.sh/b/vrtnd/openai-web-search)](https://skills.sh/vrtnd/openai-web-search)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

**Use the web research that comes with your ChatGPT/Codex subscription — in any coding
agent, outside the Codex app.**

An [Agent Skill](https://agentskills.io/specification) that works in Claude Code, Codex,
OpenCode, Cursor, Goose, Copilot, and the other clients implementing the standard.

```bash
npx skills add vrtnd/openai-web-search
```

If Codex CLI or OpenCode is already set up on your machine, that is the entire setup. No
search API key, nothing to sign up for.

## Why

**Your subscription already includes web search, and it is better than a snippet API.** The
search behind Codex does not just return links: it opens pages, finds passages inside them,
follows links across sites, reads PDFs, and cites what it used. Outside the Codex app that
capability is not exposed, so agents fall back on a separate search service — Tavily, Exa,
Brave, Serper — with its own key, its own bill, and usually snippets only.

This skill routes that same capability to any agent, three ways:

- **Direct**, using the `codex login` session already on the machine.
- **Through a proxy** such as [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI),
  or any OpenAI-compatible gateway you run — which is also how you share one subscription
  across several machines or agents.
- **Through the public OpenAI API** with an API key, when you want billing instead of a
  subscription. That path supports hosted `answer` and `research`.

## What it handles for you

Wrapping the endpoint is the easy part. These are the parts that bite:

- **Context budget.** A raw search response is 20–25 KB. Agent harnesses truncate tool
  output somewhere around 10–30 KB, so one unfiltered search can bury everything else.
  Results are summarised to about a tenth of that, with `--full` when you really want it.
- **Silent failures.** The upstream service reports stale references with HTTP 200 and
  plausible-looking prose. Those become a non-zero exit and a message that says what to do.
- **Forged citations.** Citation markers are Unicode Private Use Area characters that page
  content can contain too. Every reference is checked against the structured results, and
  uncorroborated ones are rendered `unverified` so they never get cited.

## Commands

| Command | Does |
| --- | --- |
| `research` | Research a broad topic and return a detailed report with citations and sources. |
| `answer` | Ask a question, get prose with source links. Works on every credential mode. |
| `search` | A ranked source list for manual discovery, not broad research. |
| `open` | Read a page by reference or URL, line-numbered so you can page through it. |
| `find` | Locate a passage inside an opened page. |
| `click` | Follow a numbered link from a page you already opened. |
| `raw` | The full surface: batched queries, image search, stock quotes, weather, sports, time. |
| `probe` | Report the active credential mode and what the endpoint supports. |

## Install

### npx skills (any agent)

```bash
npx skills add vrtnd/openai-web-search
```

### Claude Code plugin

```
/plugin marketplace add vrtnd/openai-web-search
/plugin install openai-web-search@openai-web-search
```

### Manual

Copy `skills/openai-web-search/` into wherever your client loads skills from — commonly
`~/.claude/skills/`, `~/.codex/skills/`, or a project-local `.agents/skills/`.

## Configure

Nothing, if Codex CLI or OpenCode is set up here. Credentials are tried in this order:

| Order | Source | Needs |
| --- | --- | --- |
| 1 | An OpenAI-compatible gateway | `WEBSEARCH_BASE_URL` + `WEBSEARCH_API_KEY` |
| 2 | OpenCode | a provider with `"npm": "@ai-sdk/openai"` in `opencode.json` |
| 3 | Codex CLI | an existing `codex login` session |
| 4 | OpenAI API | `OPENAI_API_KEY` (enables `answer` only) |

Explicit configuration wins; the rest is discovery. Check what was picked:

```bash
skills/openai-web-search/scripts/websearch probe
```

`probe` prints the mode, the endpoint, and the commands that endpoint supports, using one
request that never touches the public web. Full details in
[references/AUTH.md](skills/openai-web-search/references/AUTH.md).

## Try it

```bash
W=skills/openai-web-search/scripts/websearch

"$W" answer "What changed in the latest Kubernetes release?" --context low
"$W" research "Research current Effect TypeScript production best practices"
"$W" search "kubernetes changelog" --recency 30 --limit 5
"$W" open turn0search1 --lines 0-120
"$W" find turn0search1 "deprecation"
"$W" raw '{"weather":[{"location":"Lisbon, Portugal"}]}'
```

## Requirements

Python 3.8+ or Node.js 18+. Nothing to install — both implementations use only their
standard library, and `scripts/websearch` picks whichever is present.

## Security

This skill pulls untrusted third-party text into an agent's context, so it is built to be
audited: no dependencies, no telemetry, no network calls beyond the configured endpoint.

- Credentials come from environment variables or the Codex/OpenCode config files. They
  never appear in command-line arguments and are never logged.
- Credential files are opened read-only. The skill never refreshes or rewrites the Codex
  session, because refresh tokens rotate and doing so would break your `codex login`.
- Cross-host redirects are refused, so a redirect cannot carry a bearer token elsewhere.
- Private Use Area, bidirectional-override, and zero-width characters are stripped from all
  retrieved text.

Trust boundary: [references/SECURITY.md](skills/openai-web-search/references/SECURITY.md).
Report vulnerabilities via [SECURITY.md](SECURITY.md), not a public issue.

## Endpoint coverage

`answer` and `research` use the standard Responses API web search tool, so they work
against endpoints implementing it, including the public OpenAI API with a plain API key.
`research` is the default for broad questions: it lets the hosted model plan searches,
inspect pages, and synthesize the evidence in one request.

The page-level commands (`open`, `find`, `click`, and most of `raw`) use the Codex search
route, reached through a Codex session or a gateway that forwards it. Its command set keeps
evolving, so run `probe`: it reports exactly what the endpoint you are pointed at supports,
and the CLI turns any change into a clear message rather than a broken run.

## Development

```bash
python3 tests/run_tests.py       # shared local-mock checks, both runtimes
uvx --from skills-ref agentskills validate ./skills/openai-web-search
uvx ruff check skills/openai-web-search/scripts/websearch.py tests/
```

## Licence

[Apache-2.0](LICENSE).
