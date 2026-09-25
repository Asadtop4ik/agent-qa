#!/usr/bin/env bash
set -euo pipefail

request="${SSH_ORIGINAL_COMMAND:-}"
if [[ "$request" =~ ^deploy[[:space:]]([a-f0-9]{40})$ ]]; then
  :
elif [[ "$request" =~ ^rollback[[:space:]]([a-f0-9]{7,40})$ ]]; then
  :
else
  echo "Only deploy <full-sha> and rollback <sha> are allowed" >&2
  exit 2
fi

{ printf '%s\n' "$request"; cat; } \
  | /usr/bin/sudo -n /usr/local/sbin/agent-qa-deploy-request --request
