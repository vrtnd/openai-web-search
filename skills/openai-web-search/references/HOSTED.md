# Hosted answer and research

## Contents

- Hosted versus low-level search
- `answer`
- `research`
- Research depth
- Tool fields
- Streaming and progress
- Reading the result
- Portability

## Hosted versus low-level search

`answer` and `research` use the Responses API web search tool. The model plans searches,
inspects pages, and synthesizes evidence inside one request.

Use `research` for broad, multi-part questions such as best practices, comparisons, and
technology surveys. Use `answer` for a focused current question. Do not manually reproduce
hosted research with repeated calls to the low-level Codex search route.

The hosted layer is portable across the OpenAI API, a Codex session, and compatible
gateways that forward built-in tools. The low-level route is a separate Codex extension.

## `answer`

`answer` sends a concise instruction and lets the caller choose search context:

```json
{
  "model": "<model id>",
  "stream": true,
  "store": false,
  "instructions": "Answer using live web search. Cite each significant factual claim...",
  "input": [{
    "type": "message",
    "role": "user",
    "content": [{"type": "input_text", "text": "<question>"}]
  }],
  "tools": [{
    "type": "web_search",
    "external_web_access": true,
    "search_context_size": "high"
  }]
}
```

Use it for release changes, one factual comparison, or a narrow documentation question.

## `research`

`research` provides an explicit research workflow in the instruction, requires hosted web
search, requests complete source metadata, and uses high reasoning effort:

```json
{
  "model": "<model id>",
  "stream": true,
  "store": false,
  "reasoning": {"effort": "high"},
  "tool_choice": {"type": "web_search"},
  "include": ["web_search_call.action.sources"],
  "instructions": "Conduct source-grounded web research and return a synthesized report...",
  "input": [{
    "type": "message",
    "role": "user",
    "content": [{"type": "input_text", "text": "<research question>"}]
  }],
  "tools": [{
    "type": "web_search",
    "external_web_access": true,
    "search_context_size": "high"
  }]
}
```

The instruction tells the model to decompose the question, inspect pages instead of relying
on snippets, prefer primary sources, reconcile conflicts, separate evidence from inference,
and return a report rather than a search diary.

## Research depth

`--depth standard` is the default. It uses high context and high reasoning while keeping
the provider's normal returned-search-content budget.

`--depth deep` adds:

```json
{"return_token_budget": "unlimited"}
```

Use deep only when the task is genuinely exhaustive. It can consume substantially more
context and take several minutes. The CLI gives research a 10-minute default timeout unless
the caller supplies `--timeout`.

Deep research models such as `o3-deep-research` are a separate provider capability. This
CLI does not switch models implicitly; pass `--model` only after confirming the endpoint
offers the chosen model.

## Tool fields

| Field | Values | CLI |
| --- | --- | --- |
| `type` | `web_search` | fixed |
| `external_web_access` | live or cached | `--cached` |
| `search_context_size` | `low`, `medium`, `high` | `answer --context`; research uses high |
| `return_token_budget` | default or unlimited | `research --depth deep` |
| `filters.allowed_domains` | up to 100 domains | `--domain`, repeatable |
| `filters.blocked_domains` | up to 100 domains | `--block`, repeatable |
| `user_location` | approximate location object | not exposed |
| `search_content_types` | for example text and image | not exposed |

Domain filters omit the scheme and include subdomains.

The legacy `web_search_preview` type does not support all current fields. This skill uses
`web_search`.

## Streaming and progress

The CLI always sends `stream: true`; the ChatGPT backend requires it. SSE frames are parsed
as they arrive. Search actions are reported to stderr while final text or JSON remains on
stdout:

```text
research: running hosted web search with gpt-5.5
research: search: Effect TypeScript layers best practices
research: open_page: https://effect.website/...
```

This lets an agent distinguish a long-running research call from a stuck process.

## Reading the result

Relevant SSE events:

| Event | Carries |
| --- | --- |
| `response.output_text.delta` | Incremental answer text |
| `response.output_text.done` | Final answer text |
| `response.content_part.done` | Final text and sometimes annotations |
| `response.output_text.annotation.added` | A `url_citation` |
| `response.output_item.done` for `web_search_call` | Actions, queries, and sources |
| `response.completed` | Assembled response |

`--json` returns normalized output:

```json
{
  "text": "<report>",
  "citations": [{"url": "https://...", "title": "..."}],
  "sources": [{"url": "https://...", "title": "..."}],
  "searches": ["query one", "query two"],
  "depth": "standard"
}
```

`depth` appears on research results. JSON is never truncated. Tracking parameters appended
by upstream are removed from URLs.

Citation delivery varies across providers. The CLI collects annotation events, completed
content parts, markdown links, and requested action sources. A result with no citations is
a reason to verify important claims, not permission to assert them.

An occasional stream completes without text. Retry once.

## Portability

- `answer` and `research` work in every credential mode when the endpoint implements the
  Responses web search tool.
- The public OpenAI API supports the hosted layer but not the private low-level Codex route.
- Gateways may reject a model id or an advanced field. Set `WEBSEARCH_MODEL` or `--model`
  only to a model verified on that endpoint.
- Some gateways translate hosted search across provider protocols. Translation may not
  preserve every research option; verify with a real cited response.
