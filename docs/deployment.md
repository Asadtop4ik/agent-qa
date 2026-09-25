# Isolated Netcup deployment

The workflow builds and publishes `ghcr.io/asadtop4ik/agent-qa:<git-sha>` on each successful push to `main`. Deployment runs only after repository variable `AGENT_QA_DEPLOY_ENABLED` is set to `true`.

Before enabling deployment, provision a separate Netcup host or isolated account for this QA service. It must have Docker Engine with the Compose plugin, `curl`, a dedicated SSH user able to run Docker commands, outbound SSH access from GitHub-hosted runners, and enough disk space for commit-tagged images. The workflow uses the SSH user's `~/agent-qa` directory, transfers the image over pinned SSH, and binds the distinct `agent-qa` container to `127.0.0.1:18081`. Any public route should be configured separately to this loopback port after review.

Set these repository Actions secrets:

| Secret | Value |
| --- | --- |
| `NETCUP_HOST` | Hostname or IP of the isolated QA host |
| `NETCUP_USER` | Dedicated SSH user with Docker access |
| `NETCUP_SSH_KEY` | Private key for that user |
| `NETCUP_KNOWN_HOSTS` | Pinned `known_hosts` line for that host, verified out of band |

Set repository variable `AGENT_QA_DEPLOY_ENABLED=true` only after reviewing the host and route. The workflow does not run `ssh-keyscan` and does not accept unknown host keys.

## Rollback

The deploy script records the currently served SHA before replacing it. If a new image fails its `/ready` SHA check, it attempts to restore the previous image. For a manual rollback, connect to the isolated host and run:

```sh
cd ~/agent-qa
./deploy-agent-qa.sh rollback
```

To select a specific previously transferred image, pass its full or abbreviated Git SHA: `./deploy-agent-qa.sh rollback <sha>`. The image must still be loaded on that host. The service's ready response reports the deployed SHA for verification.
