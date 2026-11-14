# Architecture

Reelay is a layered, standard-library-only package.

```
cli.py                          flags, output, exit codes
server.py                       HTTP layer + Engine (mode routing)
matching · redact · chaos       per-request decisions
templating · upstream           response generation, forwarding
tape · events                   persistence, live traffic bus
compare · openapi · schema      offline analysis
jsonpath · jsondiff · yamlout   building blocks
models · bodies · util          data model and helpers
```

## Life of a request

1. **`_Handler`** reads the request (Content-Length or chunked transfer encoding, size-limited) and builds a
   `Request`. Paths under the control prefix (`/__reelay` by default) go to the control API instead.
2. **`Engine.handle`** asks the **chaos engine** for a decision: added delay, injected error status, dropped
   connection or bandwidth limit. Errors and drops short-circuit here.
3. The **mode router** picks a path:
   - `passthrough`: forward, return
   - `record`: forward, redact, append to the tape, index in the matcher, return the *real* response
   - `replay`: look up the matcher; on a miss apply `on_miss` (`error`, `proxy`, `record`)
   - `hybrid`: replay on a hit, record on a miss
4. **Templating** fills `{{…}}` placeholders when the matched exchange is marked `templated`.
5. A **traffic event** (redacted) is published on the **event bus**, which feeds the dashboard over server-sent
   events and the terminal log.
6. The handler writes the response, throttled when a bandwidth rule applies.

## Matching

Each request is reduced to a canonical key: the JSON encoding of the dimensions listed in `match.on` after
normalisation:

| Dimension | Normalisation |
|---|---|
| `method` | upper case |
| `path` | trailing slash removed unless `trailing_slash = true` |
| `query` | sorted pairs, ignored names removed, secrets redacted |
| `body` | JSON: parsed, ignored JSONPaths deleted. Forms: sorted pairs. Text and binary: exact |
| `headers` | the configured header names only, case-insensitive |

Exchanges with the same key form a queue, served by the strategy. On a miss, exchanges with the same method and
path (or, failing that, the same path, or anything) are scored by how many dimensions agree, and the differences
are explained.

**Redaction runs before matching on both sides.** A tape stores `Authorization: [REDACTED]`; an incoming request
with a real token is redacted the same way and therefore matches.

## Tapes

JSON Lines: a header line, then one exchange per line. Appends while recording are a single `write` plus `flush`,
so a crash loses at most the last line, and the loader tolerates a truncated final line. Rewrites (filter, merge,
redact) go through a temporary file and `os.replace`, so a tape is never half-written. See
[tape-format.md](tape-format.md).

## Bodies

Bodies are stored in their most readable lossless form: parsed JSON, text, or base64 for binary data.
Compressed upstream responses (gzip, deflate) are decoded before storage and served uncompressed, which keeps tapes
diffable. JSON bodies are re-serialised compactly on replay; `Content-Length` is always recomputed.

## Threading model

`ThreadingHTTPServer` handles each connection on a daemon thread. Shared state is guarded:

- the tape and matcher use locks, so concurrent recordings never interleave lines
- the event bus copies its subscriber list before publishing and drops events for slow subscribers instead of
  blocking the proxy
- the chaos engine serialises access to its seeded random generator, so runs are reproducible

## OpenAPI inference

1. Segments that look like identifiers (integers, UUIDs, long hex, ULIDs, long tokens with digits) become
   parameters. Positions with three or more distinct literals under the same prefix and with the same remaining
   shape become parameters too (`/orgs/acme/repos`, `/orgs/beta/repos`, `/orgs/gamma/repos`).
2. Parameter names come from the previous segment, singularised: `/users/{userId}`.
3. Bodies and responses get JSON Schemas merged across every sample (`schema.py`): union of properties, `required`
   only for keys present in every sample, `integer` widened to `number`, nullable types, string formats.
