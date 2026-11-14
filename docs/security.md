# Security notes

Reelay sits between your code and real APIs, so it handles credentials. These are the guarantees and the limits.

## Redaction

Redaction runs **before** an exchange is written to a tape, and again before traffic is shown in the dashboard or the
terminal log. Clients still receive the real, unredacted upstream response.

Default rules:

- **Headers:** `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key`, `Api-Key`,
  `X-Auth-Token`, `X-CSRF-Token`, `X-XSRF-Token`, `X-Amz-Security-Token`, `X-Goog-Api-Key`
- **Query parameters:** `token`, `access_token`, `refresh_token`, `id_token`, `api_key`, `apikey`, `key`, `secret`,
  `client_secret`, `password`, `passwd`, `signature`, `sig`, `auth`, `code`
- **JSON keys (any depth, case-insensitive):** `password`, `passwd`, `secret`, `token`, `access_token`,
  `refresh_token`, `id_token`, `client_secret`, `api_key`, `apikey`, `private_key`, `authorization`, `session`, `otp`
- **Patterns anywhere in text:** bearer tokens, JWTs, AWS access key ids, GitHub tokens, Slack tokens, Stripe secret
  keys, PEM private keys
- **Form bodies** (`application/x-www-form-urlencoded`) are treated like query strings

Redaction is a safety net, not a guarantee. Review new tapes before committing them, add your own rules for
domain-specific secrets, and use `reelay redact` to re-apply rules to existing tapes after you extend them.

## Network exposure

- Reelay listens on `127.0.0.1` by default. Binding to another address prints a warning: anyone who can reach it can
  use your upstream credentials through the proxy and read traffic in the dashboard.
- The control API changes state only for requests carrying `X-Reelay-Control: 1`. Browsers cannot add custom headers
  to cross-origin requests without a CORS preflight, which Reelay never approves for control endpoints. This blocks
  drive-by requests from web pages you visit while Reelay runs.
- `--insecure` disables upstream certificate verification and logs a warning. Use it only for internal test hosts.

## Safe evaluation

- Templates resolve against a fixed whitelist of expressions. Tape content is never executed.
- JSONPath expressions are parsed by a small hand-written parser, never evaluated as code.
- `reelay verify` only re-sends safe methods (`GET`, `HEAD`, `OPTIONS`) unless you pass `--methods` explicitly.

## Out of scope

- HTTPS interception: clients talk plain HTTP to Reelay, Reelay talks HTTPS upstream.
- Protecting tapes at rest: treat them like any other test fixture in your repository.
