# Isolated Netcup deployment

The workflow builds and publishes `ghcr.io/asadtop4ik/agent-qa:<git-sha>` on each successful push to `main`. Pushes never deploy. A Task Manager `workflow_dispatch` supplies the Task Manager run UUID, completed merge action UUID, exact PR head SHA, and exact merge SHA. Before any checkout or build, the QA workflow calls Task Manager's QA-only `qa-deployment-authorization` endpoint; it proceeds only if Task Manager confirms that the exact run and completed merge action are approved, the PR head and merge SHA match, and the run is marked dispatched. The workflow then validates the merged PR, builds the authorized merge commit, and waits for CI and image publication before deployment. Deployment also requires repository variable `AGENT_QA_DEPLOY_ENABLED=true`.

The service runs on the existing Netcup Docker host as its own `agent-qa` Compose project. Port `18081` is already used by qurbot staging, so QA uses `127.0.0.1:18082`. It has a dedicated SSH account that is not in the Docker group; a forced SSH command and a single-purpose sudo rule allow it to request only an image deploy or rollback for this service. The container runs as UID 10001 with a read-only filesystem, no capabilities, no host mounts, resource limits, and no Docker socket. Any public route should be configured separately to this loopback port after review.

This is service and account isolation on the shared Docker host, not a virtual-machine boundary. The restricted account cannot run Docker commands directly; its root-owned deploy helper accepts one image tag and only changes the `agent-qa` Compose service. The image runs without host mounts, with Docker's default seccomp profile, no capabilities, read-only filesystem, and CPU, memory, and process limits. It still shares the host kernel and Docker daemon, so a container runtime or kernel escape could affect the host.

The root-owned `/usr/local/sbin/agent-qa-deploy-request` helper and `receive_limited_archive.py` must be installed side by side. Updating the receive-limit code requires syncing both files to the host; the GitHub workflow does not install host-side helpers.

Set these repository Actions secrets:

| Secret | Value |
| --- | --- |
| `NETCUP_HOST` | Hostname or IP of the isolated QA host |
| `NETCUP_USER` | Dedicated restricted SSH account (`agentqa`) |
| `NETCUP_SSH_KEY` | Private key for that user |
| `NETCUP_KNOWN_HOSTS` | Pinned `known_hosts` line for that host, verified out of band |

Set repository variable `AGENT_QA_DEPLOY_ENABLED=true` only after reviewing the host and route. The workflow does not run `ssh-keyscan` and does not accept unknown host keys. Set variable `AGENT_QA_CALLBACK_BASE_URL=https://tasks.standart-eko.uz/api/v1` and secret `AGENT_QA_CALLBACK_TOKEN` to the same QA-only token configured in the Task Manager API. The pre-build authorization and post-deploy callback use this scoped token; the QA workflow never receives the broad `AGENT_CALLBACK_TOKEN`. The post-deploy callback runs only after the host reports the expected SHA from its loopback `/ready` endpoint. It posts the merge SHA, QA workflow URL, and readiness evidence to `/agent-runs/{agent_run_id}/qa-deployed`.

## Rollback

The deploy helper records the currently served SHA before replacing it. If a new image fails its `/ready` SHA check, it attempts to restore the previous image. For a manual rollback, open **Actions → Agent QA CI and deploy → Run workflow**, set `mode=rollback`, and enter the previously loaded SHA in `rollback_sha`. The service's ready response reports the deployed SHA for verification. This action uses the same restricted SSH account and sudo helper as deployment.
