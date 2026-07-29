# Authentication

## Contents

- Resolution order
- Mode 1: OpenAI-compatible gateway
- Mode 2: OpenCode provider
- Mode 3: Codex CLI session
- Mode 4: OpenAI API key
- Why the skill never refreshes tokens
- Environment variables
- Handling the credential file

## Resolution order

The first mode whose requirements are met wins. `websearch probe` prints the selected mode
and the reason, which is the fastest way to confirm where traffic is going.

| Order | Mode | Requires | `answer`, `research` | `search`, `open`, `find`, `click`, `raw` |
| --- | --- | --- | --- | --- |
| 1 | `gateway` | `WEBSEARCH_BASE_URL` and `WEBSEARCH_API_KEY` | yes | yes, if the gateway exposes the route |
| 2 | `opencode` | an OpenAI-shaped provider in the OpenCode config | yes | yes, if that endpoint exposes the route |
| 3 | `codex-oauth` | a Codex CLI session file | yes | yes |
| 4 | `openai-api` | `OPENAI_API_KEY` | yes | no |

Explicit configuration comes first; everything after it is discovery. Force one mode with
`--auth gateway|opencode|codex|openai` or `WEBSEARCH_AUTH`. Forcing a mode whose
requirements are unmet is an error rather than a silent fallback.

## Mode 1: OpenAI-compatible gateway

```bash
export WEBSEARCH_BASE_URL="https://gateway.example.com/v1"
export WEBSEARCH_API_KEY="..."
export WEBSEARCH_MODEL="gpt-5.6-sol"   # optional
```

The base URL must include the version path the gateway serves. Requests go to
`{base}/responses` and `{base}/alpha/search`.

Whether the second route exists depends on the gateway. `probe` reports `search layer:
unavailable` or an HTTP 404 when it does not, while hosted `answer` and `research` may
keep working.

## Mode 2: OpenCode provider

If OpenCode is configured on this machine, its provider credentials are reused. No extra
setup is needed.

The config is read from `OPENCODE_CONFIG`, else `$XDG_CONFIG_HOME/opencode/opencode.json`,
else `~/.config/opencode/opencode.json`. Providers declaring `"npm": "@ai-sdk/openai"` and
carrying both `options.baseURL` and `options.apiKey` are candidates. Providers using any
other AI SDK package are ignored, because only OpenAI-shaped ones serve the Responses API.

```json
{
  "model": "myproxy/gpt-5.6-sol",
  "provider": {
    "myproxy": {
      "npm": "@ai-sdk/openai",
      "options": {
        "baseURL": "https://gateway.example.com/v1",
        "apiKey": "..."
      }
    }
  }
}
```

### Choosing between several providers

When more than one provider qualifies, they are ranked:

1. The provider named by the top-level `model`, written as `<provider>/<model>`. This is
   the one OpenCode itself defaults to, so it is treated as the intended endpoint.
2. The order given in `enabled_providers`.
3. Name order, as a last resort.

`probe` lists the runners-up as `(also available: ...)`. If the pick was *not* settled by
the OpenCode default, a note goes to stderr naming every candidate — because at that point
the choice really is arbitrary and it decides where your credentials are sent.

Override with `WEBSEARCH_OPENCODE_PROVIDER=myproxy`, which silences the note and errors if
no provider by that name qualifies.

When the top-level `model` names the selected provider, its model part becomes the default
model for this mode.

This file holds an API key in plain text. The skill warns when it is readable beyond its
owner; `chmod 600` it.

## Mode 3: Codex CLI session

Used automatically when `$CODEX_HOME/auth.json` exists and holds a live token; `CODEX_HOME`
defaults to `~/.codex`. No API key is involved, and nothing needs configuring — install the
skill on a machine with Codex CLI signed in and it works.

Requests go straight to the ChatGPT backend at `https://chatgpt.com/backend-api/codex`,
carrying the session's access token, the account id, and an `Originator` header.

The file looks like this. Only `tokens.access_token` and `tokens.account_id` are read:

```json
{
  "auth_mode": "chatgpt",
  "OPENAI_API_KEY": null,
  "tokens": {
    "id_token": "...",
    "access_token": "...",
    "refresh_token": "...",
    "account_id": "..."
  },
  "last_refresh": "2026-07-25T11:36:59.944402Z"
}
```

The access token is a JWT. Its `exp` claim is decoded — without signature verification,
purely to check expiry — and a token inside its last minute of life counts as expired.

## Mode 4: OpenAI API key

```bash
export OPENAI_API_KEY="sk-..."
export WEBSEARCH_MODEL="gpt-5.6"   # set this if the default model id is rejected
```

Requests go to `https://api.openai.com/v1/responses`. Hosted `answer` and `research` are
available; page-level commands live on a ChatGPT backend route that the public API does not
serve.

## Why the skill never refreshes tokens

OpenAI rotates refresh tokens: a refresh returns a new one and invalidates the old. A tool
that refreshed without writing the new token back would break the user's Codex CLI login;
one that wrote it back could race with Codex CLI writing the same file.

So this skill treats the credential file as read-only. When the token has expired it exits
with code 3 and says to run `codex login`, which refreshes the session correctly and is the
user's own decision to make.

## Environment variables

| Variable | Effect |
| --- | --- |
| `WEBSEARCH_BASE_URL` | Gateway base URL, including the version path |
| `WEBSEARCH_API_KEY` | Gateway bearer token |
| `WEBSEARCH_MODEL` | Model id for every mode |
| `WEBSEARCH_AUTH` | Force `gateway`, `opencode`, `codex`, or `openai` |
| `WEBSEARCH_OPENCODE_PROVIDER` | Select a provider by name from the OpenCode config |
| `OPENCODE_CONFIG` | Path to `opencode.json`; defaults to `~/.config/opencode/opencode.json` |
| `CODEX_HOME` | Directory holding `auth.json`; defaults to `~/.codex` |
| `OPENAI_API_KEY` | OpenAI API key, hosted search only |
| `XDG_STATE_HOME` | Where session state is kept; defaults to `~/.local/state` |

Credentials are read only from the environment or the Codex file. They never appear in
command-line arguments, where any local process could read them from the process list, and
they are never logged.

## Handling the credential file

- The skill warns when `auth.json` or the OpenCode config is readable beyond its owner. Fix
  with `chmod 600`.
- It never writes to either file.
- Session state in `$XDG_STATE_HOME/openai-web-search/state.json` holds reference ids and
  URLs — no credentials — and is written atomically with mode 0600.
- Cross-host redirects are refused, so a redirect cannot carry the bearer token to another
  host.
