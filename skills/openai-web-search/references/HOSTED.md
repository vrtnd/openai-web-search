# Hosted web search reference

## Contents

- What this layer is
- Tool object fields
- Live versus cached access
- Streaming
- Reading the result
- Portability notes

## What this layer is

`websearch answer` uses the standard Responses API web search tool. The endpoint runs the
searches itself and returns prose with citations. This is the portable layer: it works
against the OpenAI API, a Codex session, and any OpenAI-compatible gateway that forwards
built-in tools.

The request the CLI builds:

```json
{
  "model": "<model id>",
  "stream": true,
  "store": false,
  "instructions": "Answer using live web search. Cite each claim with a markdown link ...",
  "input": [{"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "<question>"}]}],
  "tools": [{"type": "web_search", "external_web_access": true, "search_context_size": "low"}]
}
```

## Tool object fields

| Field | Values | CLI flag |
| --- | --- | --- |
| `type` | `web_search` | — |
| `external_web_access` | `true` (live) or `false` (cached) | `--cached` sets false |
| `search_context_size` | `low`, `medium`, `high` | `--context` |
| `filters.allowed_domains` | up to 100 domains | `--domain` (repeatable) |
| `filters.blocked_domains` | up to 100 domains | `--block` (repeatable) |
| `user_location` | `{type: "approximate", country, city, region, timezone}` | not exposed |
| `search_content_types` | e.g. `["text", "image"]` | not exposed |
| `return_token_budget` | `default` or `unlimited` | not exposed |

Domain filters omit the scheme and include subdomains.

The legacy `web_search_preview` type does not support `filters`, `external_web_access`, or
`return_token_budget`. Gateways commonly rewrite it to `web_search`.

## Live versus cached access

`external_web_access: false` restricts the tool to cached content instead of fetching
pages. It answers faster and costs less, and it is the wrong choice for anything where
currency matters.

## Streaming

The CLI always sends `stream: true` and aggregates the event stream, which is one code path
that satisfies every backend. This is not optional everywhere: the ChatGPT backend rejects
`stream: false` with `400 {"detail":"Stream must be set to true"}`. Gateways generally
accept both.

## Reading the result

Relevant events:

| Event | Carries |
| --- | --- |
| `response.output_text.delta` | Incremental answer text |
| `response.output_text.done` | Final answer text |
| `response.content_part.done` | Final text, sometimes with `annotations` |
| `response.output_text.annotation.added` | A `url_citation` |
| `response.output_item.done` (`web_search_call`) | `action.query` / `action.queries` |
| `response.completed` | The assembled response, sometimes with an empty `output` |

A `url_citation` annotation looks like:

```json
{"type": "url_citation", "start_index": 168, "end_index": 233,
 "url": "https://example.com/page", "title": "Example"}
```

Citation delivery is inconsistent across backends: annotations may arrive as events, as
part of the finished content part, or not at all. The CLI reads all of those and falls back
to extracting markdown links from the answer text, which the instructions request. Treat a
run with no citations as a reason to verify, not as a licence to assert.

Citation URLs arrive with `utm_source=openai` appended. The CLI strips it so sources are
cited canonically.

An occasional run completes with no text at all. That is transient; retry once.

## Portability notes

- `answer` works in every credential mode, including a bare `OPENAI_API_KEY`.
- Some gateways translate this tool across protocols, exposing the same capability through
  Claude-shaped or chat-completions-shaped requests. Support is uneven and can truncate
  mid-stream, so this skill always uses the Responses route.
- If a gateway rejects the model id, set `WEBSEARCH_MODEL` or pass `--model`.
