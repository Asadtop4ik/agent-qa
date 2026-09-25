# Private repository checkout credential

Task Manager's Actions workflow needs a fine-grained personal access token to check out this private repository. Create one at GitHub's fine-grained token settings with:

- Resource owner: `Asadtop4ik`
- Repository access: only `agent-qa`
- Repository permission: `Contents: read-only`
- Expiration: the shortest practical lifetime, then rotate it when it expires

The token is for reading this repository only. It must not be a classic PAT or a token copied from local `gh auth`.

This checkout credential is separate from the opt-in run credential that the Task Manager release workflow may use when it opens or fast-forwards PRs in an agent target repository. That run credential needs the target repository permissions documented by Task Manager and must be stored separately (release control names it `AGENT_QA_REPO_TOKEN`). Do not reuse the read-only checkout token for PR or content writes.

From a local checkout where `gh` is authenticated with permission to manage Actions secrets in the Task Manager repository, run:

```sh
./scripts/setup-task-manager-secret.sh <task-manager-owner/repository>
```

The script asks for the fine-grained token with terminal echo disabled, sends it directly to `gh secret set`, and stores it as `AGENT_QA_READ_TOKEN` on the named Task Manager repository. The token is not printed, put in shell history, or passed as a process argument. Confirm the repository slug before running the script. Do not paste the token into an issue, PR, chat, or workflow file.
