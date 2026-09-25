#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ! "$1" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "Usage: $0 <task-manager-owner/repository>" >&2
  exit 2
fi

target_repo="$1"
command -v gh >/dev/null || { echo "GitHub CLI (gh) is required" >&2; exit 1; }
gh auth status >/dev/null || { echo "Authenticate gh with access to manage secrets on $target_repo" >&2; exit 1; }

printf 'Paste the fine-grained token limited to Asadtop4ik/agent-qa (input hidden): '
IFS= read -r -s token
printf '\n'
if [[ -z "$token" ]]; then echo "Token cannot be empty" >&2; exit 1; fi

# Supply the PAT on stdin so it does not enter shell history or process arguments.
if printf '%s' "$token" | gh secret set AGENT_QA_READ_TOKEN --repo "$target_repo"; then
  unset token
  echo "Stored AGENT_QA_READ_TOKEN in $target_repo"
else
  unset token
  echo "Could not store the secret in $target_repo" >&2
  exit 1
fi
