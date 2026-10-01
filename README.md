# Agent QA service

Small synthetic-only HTTP service used to verify private agent repository checkout, PR CI, image build, and isolated deployment behavior.

## Local run

```sh
docker build --build-arg GIT_SHA=local -t agent-qa:local .
docker run --rm -p 8080:8080 agent-qa:local
curl http://127.0.0.1:8080/ready
```

`GET /ready` returns `200` with `{"status":"ready","git_sha":"..."}`. The SHA comes from the image build argument. `GET /fixture` returns the synthetic fixture in `data/synthetic-customer.json`.

Order write endpoints require an `X-API-Key` header. Set `AGENT_QA_API_KEY` before starting the service to configure the key. If the variable is unset or empty, the service uses the documented synthetic fallback `qa-synthetic-key`; this fallback is for synthetic QA environments only. Read endpoints and `POST /schemas/{name}/validate` remain public.

## HTTP content negotiation

Send `Accept` to choose a successful response type; routes return JSON unless
documented otherwise (`GET /metrics` returns `text/plain`). Requests that do
not accept a route's response type receive `406 not_acceptable` before the
handler runs. JSON routes still return JSON when only
`application/problem+json` is acceptable. With no `Accept` header or only
`*/*`, errors keep the `{"error":...}` envelope; when
`application/problem+json` is accepted at least as strongly as JSON, all errors
use RFC 9457 problem details. Error responses include `Vary: Accept`.

Responses with a non-empty body of at least 256 bytes use deterministic gzip
when `Accept-Encoding` permits it. Body-bearing responses vary on
`Accept-Encoding`; `GET /ready` and `GET /ping` are never compressed. The
service does not decompress request bodies; a request
with `Content-Encoding` other than `identity` receives
`415 unsupported_content_encoding` after authentication.

## API key roles

The configured bootstrap key is an immutable `admin` key. Key roles form the
`read` < `write` < `admin` hierarchy: write routes accept `write` or `admin`,
and `GET /whoami` accepts any valid key. Public routes do not need a key.
Admins can create keys with `POST /admin/keys` using `{"role":"write","label":"automation"}`, list metadata at `GET /admin/keys`, rotate a key
with `POST /admin/keys/{key_id}/rotate` and an optional `grace_seconds` from 0
to 300, or revoke it with `DELETE /admin/keys/{key_id}`. A newly created or
rotated secret is returned only in that operation's response; store it
securely. The service retains hashes in memory, and keeps at most 20 active
non-bootstrap keys. Rotation can temporarily accept the previous secret during
its grace period.

## Configuration

Admins can inspect settings through `GET /admin/config` and
`GET /admin/config/{name}`. Secret values and defaults are returned as `***`.
`POST /admin/config/validate` accepts `{"env":{"NAME":"value"}}` and checks
up to 50 values without applying them. Unknown setting names appear separately
from validation errors.

Settings are validated at startup; invalid values are reported to stderr and
stop the server with exit code 2. An empty value is treated as unset and uses
the default. Boolean values accept `true`, `false`, `1`, `0`, `yes`, and `no`,
ignoring case.

| Name | Type | Default | Accepted range | Description |
| --- | --- | --- | --- | --- |
| `APP_PORT` | int | `8080` | 1–65535 | HTTP server port. |
| `AGENT_QA_GIT_SHA` | str | `unknown` | 1–128 characters | Build revision reported by the service. |
| `AGENT_QA_API_KEY` | str (secret) | `***` | 1–4096 characters | API key required for authenticated requests. |
| `AGENT_QA_IDEMPOTENCY_TTL_SECONDS` | int | `600` | 1–86400 | Lifetime of stored idempotent responses in seconds. |
| `AGENT_QA_REQUIRE_IF_MATCH` | bool | `false` | See boolean values above | Require If-Match for conditional updates. |
| `AGENT_QA_AUDIT_CAPACITY` | int | `500` | 10–5000 | Maximum number of audit entries retained in memory. |
| `AGENT_QA_RATE_BURST` | int | `120` | 1–100000 | Token bucket request burst size. |
| `AGENT_QA_RATE_REFILL_PER_SECOND` | float | `60.0` | 0.001–10000.0 | Token bucket refill rate per second. |
| `AGENT_QA_JOB_WORKERS` | int | `2` | 1–3 | Number of lazily started background job workers. |
| `AGENT_QA_JOB_RETENTION` | int | `100` | 10–1000 | Maximum number of terminal jobs retained in memory. |

## Rate limits

Requests use an in-memory token bucket keyed by a valid API key, then a valid
`X-Client-Id`, then the client IP. Defaults are a burst of 120 requests and a
refill of 60 requests per second; set `AGENT_QA_RATE_BURST` and
`AGENT_QA_RATE_REFILL_PER_SECOND` to change them. Limited responses include
`RateLimit-*` headers, and exhausted buckets return `429 rate_limited` with
`Retry-After`. Admins can inspect policies at `GET /admin/rate-limits`, set an
override with `PUT /admin/rate-limits/{identity}` using `burst` and
`refill_per_second`, or remove one with `DELETE /admin/rate-limits/{identity}`.
Overrides and buckets are held in memory and reset when the service restarts.

## Products

`GET /products` searches and filters the in-memory product catalog;
`GET /products/{id}` reads a product, and `GET /categories` returns category
totals. Create, update, delete, and stock adjustment requests require
`X-API-Key`. Product creation accepts `sku`, `name`, `category`, and
`price_cents`, with optional `stock`, `tags`, and `active`. Use
`POST /products/{id}/adjust-stock` with `{"delta":1}` to change stock
atomically. The store holds up to 500 products and resets when the service
restarts.

For example, `GET /products?category=tools&in_stock=true&sort=-price_cents&limit=20`
lists available tools by descending price. Filters include `category`, `tag`,
`active`, `in_stock`, `min_price_cents`, `max_price_cents`, and `q`. Offset
pagination remains the default and uses `limit` and `offset`; its response has
`items`, `total`, `limit`, and `offset`. Both `/orders` and `/products` also
support keyset pagination with `pagination=cursor` (or a `cursor` parameter); its
response has `items`, `total`, `limit`, and nullable `next_cursor`, without
`offset`. A relative `Link` header points to the next page when one is available.
Pass the returned cursor on the next request and repeat the same filters and
sort. Cursor cannot be combined with `offset` or `pagination=offset`; this
returns `400 invalid_query`. Changing a filter or sort returns
`400 cursor_mismatch`; a malformed or invalidated cursor returns
`400 invalid_cursor`. Limits may change between cursor pages. Products use ID
ascending order to break sort-value ties, including descending sorts. Cursor
signatures use a process-local key, so all cursors become invalid after a
service restart.

For example, `GET /orders?status=new&sort=-id&limit=20&pagination=cursor` starts
a cursor-paginated order listing.

## Search DSL

`GET /orders/search?q=...` and `GET /products/search?q=...` search their
collections with `sort`, `limit`, and offset-based `offset` pagination. Queries
support `AND`, `OR`, `NOT`, parentheses, implicit `AND`, comparisons, `~`
substring matching, and `IN (...)`. For example,
`GET /products/search?q=category:tools%20AND%20active=true` finds active tools.
`GET /search/explain?resource=products&q=active=true` returns the normalized
query and its AST. Syntax errors use `400 invalid_search_query` and include the
zero-based character position in `details`.

## Conditional requests

Order and product item responses include opaque ETags tied to the service
process lifetime and resource version. A tag stays stable while that version is
current, changes after updates, and is regenerated after a service restart;
versions begin at 1 and are not included in JSON. Stock reservations and
releases also advance the product version. List responses use weak ETags derived
from the canonical response body. Send
`If-None-Match` on GET requests; it uses weak comparison, accepts a tag list or
`*`, and returns a bodyless `304` on a match. Send `If-Match` on PATCH, DELETE,
or stock adjustment to prevent a write based on an old version. It uses strong
comparison, accepts a tag list or `*`, and never matches a weak tag. A mismatch
returns `412` with the current ETag. A malformed condition returns `400`.
`If-Match` is optional by default. Set `AGENT_QA_REQUIRE_IF_MATCH=true` to
require it for those writes; a missing required header returns `428`.
Request checks run in this order: authentication, body parsing and validation,
resource lookup, precondition format, precondition match, then domain rules.

## Orders

`POST /orders` accepts either the legacy `{"customer_id":"...","total_cents":1500}`
shape or `{"customer_id":"...","items":[{"product_id":1,"quantity":2}]}`.
Item orders reserve stock atomically and include product name and price snapshots;
their `total_cents` is computed from the line totals. Cancelling an item order
releases its reservation once.

`POST /orders/bulk` and `POST /products/bulk` accept `{"items":[...],"atomic":false}`
with 1–50 create requests. Each result keeps its input index; the overall status
is `201` when all succeed, `207` for mixed results, and `422` when all fail.
Set `atomic` to `true` to roll back the full batch after any item fails. Bulk
request bodies are limited to 65,536 bytes.

`POST /orders` and `POST /products` accept an optional `Idempotency-Key` header
(1–64 ASCII letters, digits, `.`, `_`, `:`, or `-`). The key is scoped to the API
key, method, and exact path. Repeating the same JSON request replays its saved
2xx response with `Idempotent-Replay: true`; other create routes save only 2xx
responses, while bulk routes replay the complete result for `201`, `207`, or
`422`. An invalid key returns `400`, reuse with a different request returns
`422`, and a concurrent request returns `409`. Saved responses expire after 600
seconds by default (`AGENT_QA_IDEMPOTENCY_TTL_SECONDS`, range 1–86400), with a
maximum of 500 saved keys.

## Background jobs

`POST /jobs` queues a `sleep`, `orders_summary`, `stock_report`, or `fail` job
and returns `202` with a `Location` header. `GET /jobs` lists jobs by ascending
ID and accepts `status`, `type`, `limit`, and `offset` filters. `GET /jobs/{id}`
returns current progress and accepts `wait_ms` (0–5000) to wait for a terminal
status. `POST /jobs/{id}/cancel` cancels queued work immediately and requests
cooperative cancellation for running work. Creating and cancelling jobs require
a write API key; job reads are public. Configure workers with
`AGENT_QA_JOB_WORKERS` (1–3, default 2) and completed-job retention with
`AGENT_QA_JOB_RETENTION` (10–1000, default 100).

## Webhook outbox

`POST /webhooks` registers a simulated destination on an `.invalid` host;
`GET`, `PATCH`, and `DELETE /webhooks/{id}` manage subscriptions without
returning the signing secret. Committed order and product changes create
in-memory delivery records in `/outbox`. Admins can process due records with
`POST /outbox/process` and configure the background dispatcher at
`/outbox/dispatcher`; failed records can be requeued with
`POST /outbox/{id}/requeue`.

## JSON schemas

`GET /schemas` lists the registered schemas, `GET /schemas/{name}` returns one schema, and `POST /schemas/{name}/validate` checks any JSON value and returns `{"valid":true,"errors":[]}` or its validation errors. The same named schemas appear under `components.schemas` in `GET /openapi.json`.

## Audit log

`GET /audit` and `GET /audit/{seq}` are admin-only views of the in-memory audit
ring buffer. The list endpoint supports method, resource, actor, outcome, status,
resource ID, sequence, order, and limit filters; entries include only selected
field changes and never retain request bodies or API key values. Set
`AGENT_QA_AUDIT_CAPACITY` to configure retention (10–5000 entries, default 500).

## Layout

- `app.py` starts the HTTP service.
- `agent_qa/` contains configuration, route handlers, errors, and the server adapter.
- `data/synthetic-customer.json` is the synthetic fixture.
- `tests/` contains service and route unit tests.

## Deployment boundary

Pushes to `main` run CI and publish a commit-tagged image to GHCR. A deploy `workflow_dispatch` must first be authorized by Task Manager using the QA-only callback token, exact run UUID, completed merge action UUID, PR head SHA, and merge SHA; the workflow checks this before checkout or build. Deployment also requires `AGENT_QA_DEPLOY_ENABLED=true`. The action streams the image over a restricted SSH account with a 200 MiB receive limit; the host does not need a GHCR pull credential. The `agent-qa` Compose project keeps previous images loaded and supports rollback by SHA.

Required repository secrets and variables are documented in [docs/deployment.md](docs/deployment.md). No production customer database, token, or container is used by this project.

## Cross-private checkout credential

Task Manager Actions need a repository-scoped token to read this private repository. Follow [docs/credentials.md](docs/credentials.md) and run `scripts/setup-task-manager-secret.sh <task-manager-owner/repository>`. The script prompts for the fine-grained token without echoing it and writes it to the named repository's `AGENT_QA_READ_TOKEN` Actions secret. It never reads or copies local `gh` authentication credentials.

## agent-svc sinovi

2026-09-28
