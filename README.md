# Agent QA service

Small synthetic-only HTTP service used to verify private agent repository checkout, PR CI, image build, and isolated deployment behavior.

## Local run

```sh
docker build --build-arg GIT_SHA=local -t agent-qa:local .
docker run --rm -p 8080:8080 agent-qa:local
curl http://127.0.0.1:8080/ready
```

`GET /ready` returns `200` with `{"status":"ready","git_sha":"..."}`. The SHA comes from the image build argument. `GET /fixture` returns the synthetic fixture in `data/synthetic-customer.json`.

## Deployment boundary

The deployment workflow publishes a commit-tagged image to GHCR on pushes to `main`. It deploys only after `AGENT_QA_DEPLOY_ENABLED=true` is set as a repository variable and the Netcup secrets are configured. The workflow transfers the built image to a separately named `agent-qa` service; the Netcup host does not need a GHCR pull credential. The deployment script keeps the last deployed SHA and supports rollback to a previously loaded image.

Required repository secrets and variables are documented in [docs/deployment.md](docs/deployment.md). No production customer database, token, or container is used by this project.

## Cross-private checkout credential

Task Manager Actions need a repository-scoped token to read this private repository. Follow [docs/credentials.md](docs/credentials.md) and run `scripts/setup-task-manager-secret.sh <task-manager-owner/repository>`. The script prompts for the fine-grained token without echoing it and writes it to the named repository's `AGENT_QA_READ_TOKEN` Actions secret. It never reads or copies local `gh` authentication credentials.
