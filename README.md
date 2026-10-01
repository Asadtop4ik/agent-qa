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

## JSON schemas

`GET /schemas` lists the registered schemas, `GET /schemas/{name}` returns one schema, and `POST /schemas/{name}/validate` checks any JSON value and returns `{"valid":true,"errors":[]}` or its validation errors. The same named schemas appear under `components.schemas` in `GET /openapi.json`.

## Content negotiation

Send `Accept: application/problem+json` to receive RFC 9457 error responses when
its quality value is at least as high as the matching JSON alternatives. With no
`Accept` header, or only `*/*`, errors keep the standard `{"error":...}` envelope.
Routes return `406 not_acceptable` before running a handler when none of their
response types are acceptable, so rejected write requests have no side effects.
`Accept-Encoding: gzip` enables deterministic gzip for response bodies of at
least 256 bytes. `/ready` and `/ping` stay uncompressed. Requests may use only
`Content-Encoding: identity`; another encoding returns `415` after
authentication and before the body is read.

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
