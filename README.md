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
`active`, `in_stock`, `min_price_cents`, `max_price_cents`, and `q`; pagination
uses `limit` and `offset`.

## Orders

`POST /orders` accepts either the legacy `{"customer_id":"...","total_cents":1500}`
shape or `{"customer_id":"...","items":[{"product_id":1,"quantity":2}]}`.
Item orders reserve stock atomically and include product name and price snapshots;
their `total_cents` is computed from the line totals. Cancelling an item order
releases its reservation once.

## JSON schemas

`GET /schemas` lists the registered schemas, `GET /schemas/{name}` returns one schema, and `POST /schemas/{name}/validate` checks any JSON value and returns `{"valid":true,"errors":[]}` or its validation errors. The same named schemas appear under `components.schemas` in `GET /openapi.json`.

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
