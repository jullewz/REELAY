# Tape format (version 1)

A tape is a UTF-8 [JSON Lines](https://jsonlines.org) file. Line 1 is the header, every other non-empty line is one
exchange, in recording order.

## Header

```json
{"reelay_tape": 1, "name": "github", "created_at": "2026-11-14T01:00:00.000Z", "upstream": "https://api.github.com"}
```

| Field | Type | Notes |
|---|---|---|
| `reelay_tape` | integer | Format version, must be `1` |
| `name` | string | Display name |
| `created_at` | string | ISO 8601 |
| `upstream` | string or null | Base URL the traffic was recorded from |
| `meta` | object | Optional, free-form |

## Exchange

```json
{
  "id": "01JD3K5Z7Q2W4X6Y8A0B2C4D6E",
  "recorded_at": "2026-11-14T01:00:01.250Z",
  "duration_ms": 182.4,
  "upstream": "https://api.github.com",
  "request": {
    "method": "GET",
    "path": "/users/octocat",
    "query": [["per_page", "10"]],
    "headers": [["Accept", "application/json"], ["Authorization", "[REDACTED]"]],
    "body": {"encoding": "empty"}
  },
  "response": {
    "status": 200,
    "reason": "OK",
    "headers": [["Content-Type", "application/json; charset=utf-8"]],
    "body": {"encoding": "json", "content": {"login": "octocat", "id": 583231}}
  },
  "tags": ["smoke"],
  "templated": false
}
```

- `id`: 26-character sortable identifier (ULID layout). Commands accept unique prefixes.
- `query` and `headers` are lists of `[name, value]` pairs, so order and repeated names are preserved.
- `tags` and `templated` are optional.

## Bodies

| `encoding` | `content` |
|---|---|
| `empty` | absent |
| `json` | any JSON value |
| `text` | string |
| `base64` | standard base64 string |

## Editing by hand

Tapes are meant to be edited: change a response, add `"templated": true`, delete lines. Keep one exchange per line.
`reelay ls` and `reelay show` validate the file and report the line number of any problem.
