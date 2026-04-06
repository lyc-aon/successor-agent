# llama.cpp Protocol Reference

What we send to llama.cpp's HTTP server, what we get back, and the
quirks Lycaon's local Qwen3.5-27B-Opus-Distilled-v2 setup specifically.
This doc exists so future Claude doesn't have to re-probe the API to
figure out the response shape — read this first when touching anything
in `src/ronin/providers/llama.py`.

Probed against `b1-ecd99d6` (llama.cpp build identifier from the
`system_fingerprint` field of the response, ~April 2026).

---

## The server

Lycaon's local llama.cpp setup runs:

```
llama-server \
  -m /home/lycaon/models/Qwen3.5-27B-Opus-Distilled-v2-Q4_K_M.gguf \
  --host 0.0.0.0 \
  --port 8080 \
  -ngl 99 \
  -c 262144 \
  -ctk q8_0 \
  -ctv q8_0 \
  -fa on \
  --temp 0.7
```

- **Model**: Qwen3.5 27B distilled from Opus, Q4_K_M quant (16.5 GB)
- **Context**: 262144 tokens (256K) — see
  [`feedback_local_inference_generous_defaults.md`](../../../.claude/projects/-home-lycaon/memory/feedback_local_inference_generous_defaults.md)
  for the user's directive to use generous token budgets
- **GPU layers**: 99 (full offload to RTX 5090)
- **KV cache**: Q8_0 quantized (saves VRAM)
- **Flash attention**: on
- **Host**: `0.0.0.0:8080`, accessible as `http://localhost:8080`

Throughput on this hardware: **~49 tokens/sec** generation, ~307
tokens/sec prompt eval (measured 2026-04-06).

---

## Endpoints we use

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | quick `{"status":"ok"}` heartbeat |
| `/v1/models` | GET | list loaded models, returns alias dict |
| `/v1/chat/completions` | POST | OpenAI-compatible chat completion |

There's also `/completion` (raw prompt + token stream, llama.cpp-native)
and `/v1/embeddings` etc. but we don't use them in Ronin.

---

## Request format — `POST /v1/chat/completions`

```http
POST /v1/chat/completions HTTP/1.1
Content-Type: application/json
Accept: text/event-stream
```

```json
{
  "model": "qwopus",
  "messages": [
    {"role": "system",    "content": "You are ronin..."},
    {"role": "user",      "content": "Greet me."},
    {"role": "assistant", "content": "Greetings, traveler."},
    {"role": "user",      "content": "What's your blade?"}
  ],
  "stream": true,
  "max_tokens": 32768,
  "temperature": 0.7
}
```

### Field notes

| Field | Required | Notes |
|---|---|---|
| `model` | yes | **Ignored by llama.cpp** — any string works. We send `"qwopus"` for clarity in logs. |
| `messages` | yes | Standard OpenAI shape. Roles: `system`, `user`, `assistant`. Note we map our internal `"ronin"` role to `"assistant"` before sending. |
| `stream` | optional | `true` for SSE, `false` for one-shot JSON. Ronin always streams. |
| `max_tokens` | optional | **Use generous values** (16K-32K). The 256K context easily handles it. |
| `temperature` | optional | 0.0-2.0; we default to 0.7. |
| `top_p` | optional | Nucleus sampling. |
| `top_k` | optional | Top-k sampling. |
| `repeat_penalty` | optional | llama.cpp-specific; OpenAI calls it `frequency_penalty`. |
| `min_p` | optional | llama.cpp-specific min-p sampling. |
| `seed` | optional | Reproducible sampling. |

The `extra` parameter on `LlamaCppClient.stream_chat` lets you pass
any additional knobs through to the request body.

### System prompt handling

Qwen3.5-27B-Opus-Distilled-v2 is a thinking model. It tends to add
"Solution:", "Verification:", and checkmark lists to its replies if
not explicitly told not to. The Ronin chat system prompt includes
explicit instructions to suppress these patterns:

```
You are ronin — a terse, contemplative wandering samurai assistant.
Speak as ronin would: with intention, with brevity...
Do not use markdown headers. Do not use bullet lists or numbered lists.
Do not write "Solution:", "Answer:", "Verification:", "Note:", or any
preamble label. Do not use checkmarks. Do not wrap your reply in code
fences unless the user asked for code.
```

This is a model-specific quirk; other models may not need it.

---

## Response format — non-streaming (`stream: false`)

```json
{
  "choices": [
    {
      "finish_reason": "stop",
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Greetings, traveler.",
        "reasoning_content": "Let me think through this carefully..."
      }
    }
  ],
  "created": 1775516869,
  "model": "Qwen3.5-27B-Opus-Distilled-v2-Q4_K_M.gguf",
  "system_fingerprint": "b1-ecd99d6",
  "object": "chat.completion",
  "usage": {
    "completion_tokens": 124,
    "prompt_tokens": 34,
    "total_tokens": 158
  },
  "id": "chatcmpl-...",
  "timings": {
    "cache_n": 0,
    "prompt_n": 34,
    "prompt_ms": 110.645,
    "prompt_per_token_ms": 3.254,
    "prompt_per_second": 307.289,
    "predicted_n": 64,
    "predicted_ms": 1302.927,
    "predicted_per_token_ms": 20.358,
    "predicted_per_second": 49.120
  }
}
```

### Key fields

- `choices[0].message.content` — the user-visible answer
- `choices[0].message.reasoning_content` — **the model's internal
  thinking, separate from content**. This is a Qwen3.5 thinking-model
  feature; not present on non-thinking models.
- `choices[0].finish_reason` — `"stop"` (natural end), `"length"`
  (hit max_tokens), `"content_filter"` (rare)
- `usage` — standard token counts
- `timings` — llama.cpp-specific perf info; useful for tokens/sec gauges

**Trap**: if `max_tokens` is too small (say 64) and the model is in a
thinking mood, the entire token budget gets consumed by reasoning and
`content` comes back as an empty string. We learned this the hard way.
Use generous `max_tokens` (16K+).

---

## Response format — streaming (`stream: true`)

The server sends Server-Sent Events. Each event is a `data:` line
followed by an empty line:

```
data: {"choices":[...],"created":...,"id":"...","object":"chat.completion.chunk"}

data: {"choices":[...],"created":...,"id":"...","object":"chat.completion.chunk"}

data: [DONE]
```

The stream **always ends with `data: [DONE]`** (or an EOF on the
underlying connection).

### Chunk shapes

**First chunk** (sets the role):
```json
{
  "choices": [{
    "finish_reason": null,
    "index": 0,
    "delta": {"role": "assistant", "content": null}
  }],
  "created": 1775516885,
  "id": "chatcmpl-...",
  "model": "Qwen3.5-27B-Opus-Distilled-v2-Q4_K_M.gguf",
  "system_fingerprint": "b1-ecd99d6",
  "object": "chat.completion.chunk"
}
```

**Reasoning delta** (Qwen3.5 thinking mode — the model emits these
first, often hundreds of them, before any user-visible content):
```json
{"choices":[{
  "finish_reason": null,
  "index": 0,
  "delta": {"reasoning_content": " step"}
}], ...}
```

**Content delta** (the user-visible answer — arrives after reasoning):
```json
{"choices":[{
  "finish_reason": null,
  "index": 0,
  "delta": {"content": "Greetings"}
}], ...}
```

**Final chunk** (`finish_reason` set):
```json
{"choices":[{
  "finish_reason": "stop",
  "index": 0,
  "delta": {}
}], ...}
```

The `[DONE]` sentinel comes after this on its own data line.

### The reasoning vs content split

This is the most important thing about thinking models:

> **`reasoning_content` and `content` are two separate channels in the
> same stream.** Both arrive via `delta.<channel>` in the same SSE
> chunks. A given chunk has either `reasoning_content` OR `content`,
> never both. There is no explicit "transition" event — you just
> notice that chunks switch from `reasoning_content` to `content`.

Implication for the chat UX:

- During the reasoning phase, the `_stream_content` accumulator stays
  empty. The renderer shows a "thinking" spinner with a live char
  count of accumulated reasoning. The user sees that the model is
  working.
- The first `content` delta is the silent transition. The renderer
  detects "we have content now, hide the spinner" and starts the
  typewriter animation.
- The reasoning is **never shown** to the user in the v0 chat. Future
  toggle could surface it as a dim secondary lane.

### Where `usage` and `timings` show up in the stream

Inconsistent. Sometimes the final chunk includes a `"usage": {...}`
field alongside the `delta`, sometimes a separate chunk after the
finish_reason chunk has `usage` with no `choices`, sometimes neither
and you have to count tokens yourself.

Ronin's `ChatStream._read_sse` handles all three cases:
1. Final chunk with `finish_reason` AND `usage` → captured
2. Trailing chunk with `usage` and no `choices` → captured
3. No usage at all → fall back to char-count estimate in the ctx bar

Same for `timings` (llama.cpp-specific perf info).

---

## Error responses

### HTTP errors

```http
HTTP/1.1 400 Bad Request
Content-Type: application/json

{"error": {"message": "...", "type": "invalid_request", ...}}
```

`urllib.error.HTTPError` catches these in the worker thread and
emits a `StreamError(message="HTTP 400: Bad Request")`.

### Connection errors

If llama.cpp is down: `urllib.error.URLError` →
`StreamError(message="connection failed: ...")`.

### Stream interrupted mid-response

The chat App can call `ChatStream.close()` (e.g., on Ctrl+G or process
exit). The worker thread sees `_stop.is_set()` on its next readline
boundary and emits `StreamEnded(finish_reason="cancelled", ...)` with
whatever content has accumulated so far.

**Note**: this only stops *consuming* the stream from llama.cpp. It
doesn't actually tell llama.cpp to stop generating (there's no
"abort" in the OpenAI API). The model keeps generating into the void
until it hits its own stop condition or fills max_tokens.

To actually free server resources, we'd need a separate llama.cpp
endpoint or to forcefully close the underlying socket. For v0, we
just stop reading and accept the wasted tokens (it's local, free).

---

## Practical performance numbers

Measured on Lycaon's RTX 5090 / Qwen3.5-27B-Opus-Distilled-v2 / Q4_K_M:

| Metric | Value |
|---|---|
| Prompt eval | ~307 tokens/sec |
| Generation | ~49 tokens/sec |
| First token latency | ~50-150 ms (warm cache) |
| Cold start | ~3-5 sec (first request after server start) |
| Reasoning chars before content (typical short query) | 200-500 |
| Reasoning chars before content (complex query) | 1000-5000 |

For the chat UX this means:
- The "thinking" spinner phase is typically **2-15 seconds** for
  thinking-mode replies
- Content streaming is **fast** (49 tok/sec is faster than reading
  speed) so the typewriter rarely needs throttling
- A typical full response cycle (prompt → reasoning → content → done)
  is **3-30 seconds** depending on complexity

The chat's `ChatStream.poll()` is called every frame at 30 FPS, so
worst-case latency between a delta arriving in the queue and the
user seeing it on screen is ~33 ms. Effectively real-time.

---

## How Ronin uses this

The full pipeline:

```
RoninChat._submit()
  ├─ build api_messages from self.messages (skip synthetic)
  ├─ self._stream = self.client.stream_chat(messages=api_messages)
  └─ ChatStream worker thread starts
       └─ POSTs to /v1/chat/completions
            └─ urllib.request.urlopen() with Accept: text/event-stream
                 └─ readline loop parses SSE chunks
                      └─ for each chunk:
                           ├─ if delta.reasoning_content: append + emit ReasoningChunk
                           ├─ if delta.content: append + emit ContentChunk
                           └─ if finish_reason: capture usage/timings, emit StreamEnded

RoninChat.on_tick (every frame, 30 FPS)
  ├─ self._pump_stream() drains all queued events
  │    ├─ ReasoningChunk → bumps _stream_reasoning_chars counter
  │    ├─ ContentChunk → appends to _stream_content list
  │    ├─ StreamEnded → commits content as a _Message, clears stream state
  │    └─ StreamError → commits partial as a synthetic error message
  └─ self._paint_chat_area renders the stream as a "virtual message"
       at the bottom of the chat area (only visible when scroll_offset == 0)
```

---

## Files

| File | Role |
|---|---|
| `src/ronin/providers/llama.py` | The client + ChatStream + event types |
| `src/ronin/providers/__init__.py` | Re-exports for convenience |
| `src/ronin/demos/chat.py` | The first consumer of LlamaCppClient |

---

## What this doc deliberately does NOT cover

These are llama.cpp features Ronin doesn't use yet, so we haven't
probed them:

- **Tool calls** — llama.cpp's chat-completions endpoint supports
  OpenAI-style `tools` and `tool_choice`. Qwen3.5-Coder has been
  trained for tool use; Qwen3.5-27B-Opus-Distilled is less reliable
  at it. We'll cross this bridge when we add the agent loop.
- **Multimodal / image input** — the server has the mmproj-Qwen3.5-27B
  multimodal projector loaded. Needs `/v1/chat/completions` with
  `image_url` content blocks.
- **`/completion` endpoint** — raw prompt + token-level streaming
  with logprobs. More powerful but llama.cpp-specific. We'd use this
  if we needed token-level control.
- **`/v1/embeddings`** — for RAG / vector search. Different model needed.
- **`/slots` and `/slots/<id>` endpoints** — manage parallel slots.
  Useful when running multiple sessions against the same server.
- **Request cancellation via `/slots/<id>/erase`** — actually cancel
  generation server-side instead of just dropping our connection.

When any of these become relevant, probe with `curl` first, document
the response shape here, then build the client.
