# Configuration reference

Reelay reads `reelay.toml` from `--config PATH`, `$REELAY_CONFIG` or the current directory. The file is optional,
and command-line flags override it. Create a commented starter with `reelay init`, check it with `reelay config`.

## `[server]`

| Key | Default | Flag | Description |
|---|---|---|---|
| `host` | `"127.0.0.1"` | `--host` | Listen address. Use `0.0.0.0` only on trusted networks |
| `port` | `8080` | `--port` | Listen port. `0` picks a free port |
| `mode` | `"replay"` | `--mode` | `record`, `replay`, `hybrid`, `passthrough` |
| `tape` | none | `--tape` | Tape file |
| `target` | none | `--target` | Upstream base URL, may include a base path: `https://host/api/v2` |
| `on_miss` | `"error"` | `--on-miss` | In replay mode: `error` (501 with diagnostics), `proxy`, `record` |
| `timeout` | `"30s"` | | Upstream timeout |
| `verify_tls` | `true` | `--insecure` | Verify upstream certificates |
| `max_body_size` | `"50MiB"` | | Larger requests get `413`, larger upstream responses fail with `502` |
| `control_prefix` | `"/__reelay"` | | Path prefix for the dashboard and control API |
| `dashboard` | `true` | `--no-dashboard` | Serve the live dashboard |
| `cors` | `false` | `--cors` | Permissive CORS headers and automatic preflight answers |

## `[match]`

| Key | Default | Description |
|---|---|---|
| `on` | `["method", "path", "query", "body"]` | Dimensions that must be equal. Add `"headers"` to match headers |
| `headers` | `[]` | Header names compared when `"headers"` is in `on`, for example `["Accept"]` |
| `ignore_query` | `[]` | Query parameter names left out of matching |
| `ignore_body` | `[]` | JSONPaths removed from JSON bodies before matching |
| `strategy` | `"sequential"` | `sequential` (in order, then stick to the last), `cycle`, `first`, `last` |
| `trailing_slash` | `false` | Treat `/a/` and `/a` as different paths |
| `fuzzy` | `false` | On a miss, serve the closest recording (`X-Reelay-Match: fuzzy`) |

### JSONPath subset

`$` root, `.key` or `['key']` member, `[0]` / `[-1]` index, `[*]` / `.*` wildcard, `..key` recursive descent.

## `[redact]`

Defaults always apply unless `keep_defaults = false`. See [security.md](security.md) for the default lists.

| Key | Description |
|---|---|
| `enabled` | `true` by default |
| `headers` | Extra header names to blank |
| `query` | Extra query parameter names |
| `json_keys` | Extra JSON keys, matched case-insensitively at any depth |
| `json_paths` | Exact JSONPaths to blank, for example `["$.card.number"]` |
| `patterns` | Extra regular expressions, applied to header values, query values, text bodies and JSON strings |
| `placeholder` | Replacement text, default `[REDACTED]` |
| `keep_defaults` | Set `false` to use only your own lists |

## `[chaos]` and `[[chaos.rule]]`

```toml
[chaos]
enabled = true     # also --chaos / --no-chaos
seed = 42          # also --seed; same seed, same faults

[[chaos.rule]]
route = "GET /search/**"     # first matching rule wins
latency = "100ms..600ms"     # "250ms", "100ms..600ms", "normal(200ms,50ms)"
error_rate = 0.05            # 0..1, answer with error_status instead
error_status = 503
drop_rate = 0.01             # 0..1, close the connection without answering
bandwidth = "64KiB/s"        # throttle the response body
```

### Route syntax

`"[METHOD[|METHOD…]] /path/glob"`: `*` matches one segment, `**` any number. `"/health"` matches every method,
`"GET|HEAD /static/**"` two of them, `"**"` everything.

## `[diff]`

| Key | Description |
|---|---|
| `ignore` | JSONPaths ignored by `reelay diff` and `reelay verify`, added to `--ignore` flags |

## Templates

Mark an exchange `"templated": true` in the tape to enable placeholders in its response headers and body:

| Expression | Value |
|---|---|
| `request.method`, `request.path` | method, path |
| `request.path.N` | path segment N (0-based) |
| `request.query.NAME`, `request.header.NAME` | query parameter, header |
| `request.json.a.b.0` | field from the JSON request body |
| `uuid`, `now`, `now_unix`, `seq`, `random_int(A,B)` | generated values |

A JSON string that is exactly one placeholder keeps the type of the value (`"{{request.json.count}}"` → `3`).
