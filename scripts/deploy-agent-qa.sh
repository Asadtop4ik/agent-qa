#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$APP_DIR/docker-compose.yml"
STATE_FILE="$APP_DIR/.current_sha"
PREVIOUS_FILE="$APP_DIR/.previous_sha"
READY_URL="${AGENT_QA_READY_URL:-http://127.0.0.1:18081/ready}"
IMAGE_PREFIX="ghcr.io/asadtop4ik/agent-qa"

usage() {
  echo "Usage: $0 deploy <git-sha> | rollback [git-sha]" >&2
  exit 2
}

valid_sha() { [[ "$1" =~ ^[a-f0-9]{7,40}$ ]]; }
image_for() { printf '%s:%s' "$IMAGE_PREFIX" "$1"; }

ready_sha() {
  curl --fail --silent --show-error --max-time 2 "$READY_URL" 2>/dev/null \
    | sed -n 's/.*"git_sha":"\([^"]*\)".*/\1/p' || true
}

wait_ready() {
  local expected="$1" attempt actual
  for attempt in $(seq 1 30); do
    actual="$(ready_sha)"
    if [[ "$actual" == "$expected" ]]; then return 0; fi
    sleep 1
  done
  echo "agent-qa did not report expected SHA $expected (last: ${actual:-unavailable})" >&2
  return 1
}

start_sha() {
  local sha="$1"
  AGENT_QA_IMAGE="$(image_for "$sha")" AGENT_QA_SHA="$sha" \
    docker compose -f "$COMPOSE_FILE" up -d --no-deps agent-qa
  wait_ready "$sha"
}

deploy() {
  local sha="$1" old_sha
  valid_sha "$sha" || { echo "Invalid git SHA: $sha" >&2; exit 2; }
  docker image inspect "$(image_for "$sha")" >/dev/null
  old_sha="$(ready_sha)"
  if [[ -z "$old_sha" && -f "$STATE_FILE" ]]; then old_sha="$(cat "$STATE_FILE")"; fi
  [[ "$old_sha" == "$sha" ]] && { echo "agent-qa already runs $sha"; return; }

  if start_sha "$sha"; then
    if [[ -n "$old_sha" ]] && valid_sha "$old_sha"; then printf '%s\n' "$old_sha" > "$PREVIOUS_FILE"; fi
    printf '%s\n' "$sha" > "$STATE_FILE"
    echo "agent-qa deployed $sha"
    return
  fi

  if [[ -n "$old_sha" ]] && valid_sha "$old_sha"; then
    echo "Deployment failed; restoring $old_sha" >&2
    if docker image inspect "$(image_for "$old_sha")" >/dev/null 2>&1 && start_sha "$old_sha"; then
      printf '%s\n' "$old_sha" > "$STATE_FILE"
    else
      echo "Automatic restore failed; use rollback after loading the previous image" >&2
    fi
  fi
  return 1
}

rollback() {
  local sha="${1:-}"
  if [[ -z "$sha" ]]; then
    [[ -f "$PREVIOUS_FILE" ]] || { echo "No previous SHA recorded" >&2; exit 1; }
    sha="$(cat "$PREVIOUS_FILE")"
  fi
  valid_sha "$sha" || { echo "Invalid git SHA: $sha" >&2; exit 2; }
  docker image inspect "$(image_for "$sha")" >/dev/null
  local current_sha
  current_sha="$(ready_sha)"
  start_sha "$sha"
  if [[ -n "$current_sha" ]] && valid_sha "$current_sha"; then printf '%s\n' "$current_sha" > "$PREVIOUS_FILE"; fi
  printf '%s\n' "$sha" > "$STATE_FILE"
  echo "agent-qa rolled back to $sha"
}

[[ $# -ge 1 ]] || usage
case "$1" in
  deploy) [[ $# -eq 2 ]] || usage; deploy "$2" ;;
  rollback) [[ $# -le 2 ]] || usage; rollback "${2:-}" ;;
  *) usage ;;
esac
