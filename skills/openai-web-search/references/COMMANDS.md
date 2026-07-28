# Search command reference

## Contents

- Request shape
- Commands: search_query, image_query, open, click, find, screenshot
- Commands: finance, weather, sports, time
- response_length
- Settings
- Response shape and reference ids
- Constraints and gotchas
- Failures that arrive as HTTP 200

This documents the command layer reached by `search`, `open`, `find`, `click`, and `raw`.
It is available through a Codex session or a gateway that forwards the route, and not
through the public OpenAI API. The command set keeps evolving, so run `probe` when
behaviour looks unexpected — it reports what the endpoint currently accepts.

`raw` takes the whole command object, so anything described here is reachable even when no
dedicated flag exists.

## Request shape

The CLI builds this; `raw` supplies the `commands` value.

```json
{
  "id": "<session uuid>",
  "model": "<model id>",
  "commands": { "search_query": [{ "q": "example" }], "response_length": "short" },
  "settings": { "external_web_access": true }
}
```

`id` and `model` are both required; omitting either is an HTTP 400. Several commands may
appear in one object and run together, which is faster than issuing them one at a time.

## search_query

Query the web.

```json
{"search_query": [{"q": "rust 1.90 release notes", "recency": 30, "domains": ["blog.rust-lang.org"]}]}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `q` | yes | Query text |
| `recency` | no | Restrict to the last N days |
| `domains` | no | Restrict to these domains |

## image_query

Same fields as `search_query`, against an image index. Results appear in `output` only —
the structured `results` array stays empty.

## open

```json
{"open": [{"ref_id": "turn0search1", "lineno": 120}]}
```

`ref_id` accepts a reference id or a fully qualified URL, so a known page needs no prior
search. `lineno` positions the viewport. Pages come back with line numbers (`L0:`, `L1:`);
PDFs use `L{line}@P{page}` and report their page count.

## click

```json
{"click": [{"ref_id": "turn2view0", "id": 6}]}
```

Follows a numbered link from a page that was already opened in this session. Valid ids are
the ones the CLI renders as `{link 6: label -> domain}`.

## find

```json
{"find": [{"ref_id": "turn2view0", "pattern": "deprecation"}]}
```

Locates a pattern in an opened page. Cheaper and more precise than paging through it.

## screenshot

```json
{"screenshot": [{"ref_id": "turn1view0", "pageno": 0}]}
```

PDF pages only, zero-indexed. **Not exposed as a CLI command**: through this API the
response carries a reference but no usable image, because the image is delivered
out-of-band to the official client. Use `open` on the PDF and read the text instead.

## finance

```json
{"finance": [{"ticker": "NVDA", "type": "equity", "market": "USA"}]}
```

`type` is one of `equity`, `fund`, `crypto`, `index`. `market` is an ISO 3166-1 alpha-3
code, `"OTC"`, or `""` for cryptocurrency.

## weather

```json
{"weather": [{"location": "Lisbon, Portugal", "start": "2026-08-01", "duration": 7}]}
```

`start` defaults to today and `duration` to 7 days. Returns current conditions, a daily
forecast, and any severe-weather alerts.

## sports

```json
{"sports": [{"tool": "sports", "fn": "standings", "league": "nba"}]}
```

`fn` is `schedule` or `standings`. `league` is one of `nba`, `wnba`, `nfl`, `nhl`, `mlb`,
`epl`, `ncaamb`, `ncaawb`, `ipl`. Optional: `team` and `opponent` as broadcast-style
abbreviations, `date_from`, `date_to`, `num_games`, `locale`.

**`tool: "sports"` is required by the endpoint** even though published client schemas mark
it optional. Omitting it fails — which is exactly what the capability probe exploits.

## time

```json
{"time": [{"utc_offset": "+03:00"}]}
```

## response_length

`short`, `medium`, or `long`, defaulting to `medium` upstream and to `short` in this CLI.
It changes how much text each result carries, not how many results come back: `long`
roughly doubles the payload of `short` for an identical result set. It belongs inside
`commands`, not `settings` — putting it in `settings` is an HTTP 400.

## Settings

```json
{"settings": {"external_web_access": false, "search_context_size": "low"}}
```

`external_web_access: false` is not an error. It switches to a cached index: results come
back as `news` references with a smaller per-source word budget rather than freshly
fetched pages. Use it when currency does not matter.

## Response shape and reference ids

```json
{"encrypted_output": "...", "output": "<text with markers>", "results": [...]}
```

`output` is prose meant for a model. `results` is structured and preferred wherever it is
populated:

```json
{"type": "text_result", "ref_id": "turn1search0", "url": "https://...",
 "title": "...", "domain": "example.com", "snippet": "..."}
```

`results` is populated for web results and empty for `time`, `weather`, `sports`,
`finance`, `image_query`, and `screenshot` — for those, read `output`.

Reference ids are `turn{N}{kind}{M}`, where `N` counts successful calls in the session and
`kind` reflects the command: `search`, `news` (cached mode), `view` (open, click, find),
`forecast`, `sports`, `time`, `finance`, `image`.

## Constraints and gotchas

- Keep `search_query` to at most 4 entries per call. Beyond 3, set `response_length` to
  `medium` or `long`.
- A single web search returns roughly 20-25 KB. Summarise before showing anything.
- Unknown fields are rejected outright with HTTP 400 — do not send speculative parameters.
- Citation markers use Private Use Area delimiters and are stripped by the CLI. See
  [SECURITY.md](SECURITY.md).

## Failures that arrive as HTTP 200

Several failures come back with a success status and plausible-looking text. The CLI
detects these and exits 5; when calling the API directly, check for them explicitly.

| Signature | Cause |
| --- | --- |
| `results[0].title == "Internal Error"` with no `url` | Usually a stale or foreign reference id |
| `output` starts with `Unable to resolve open call` | The reference id does not belong to this session |
| `output` starts with `Error parsing function call` | Malformed command object; the text includes the endpoint's own schema |
| `output` starts with `Found no tool response` | Arguments were the right shape but invalid |
