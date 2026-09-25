#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 1 && "$1" == --request ]] || { echo "Invalid invocation" >&2; exit 2; }

read -r request || { echo "Missing deployment request" >&2; exit 2; }
read -r action sha extra <<<"$request"
[[ -z "${extra:-}" ]] || { echo "Unexpected deployment request fields" >&2; exit 2; }

APP_DIR="/opt/agent-qa"
COMPOSE_FILE="$APP_DIR/docker-compose.yml"
STATE_FILE="$APP_DIR/.current_sha"
PREVIOUS_FILE="$APP_DIR/.previous_sha"
READY_URL="http://127.0.0.1:18082/ready"
IMAGE_PREFIX="ghcr.io/asadtop4ik/agent-qa"

valid_sha() { [[ "$1" =~ ^[a-f0-9]{7,40}$ ]]; }
image_for() { printf '%s:%s' "$IMAGE_PREFIX" "$1"; }
ready_json() { curl --fail --silent --show-error --max-time 2 "$READY_URL" 2>/dev/null || true; }
ready_sha() { ready_json | sed -n 's/.*"git_sha":"\([^"]*\)".*/\1/p'; }

wait_ready() {
  local expected="$1" attempt json actual
  for attempt in $(seq 1 30); do
    json="$(ready_json)"
    actual="$(printf '%s' "$json" | sed -n 's/.*"git_sha":"\([^"]*\)".*/\1/p')"
    if [[ "$actual" == "$expected" ]]; then return 0; fi
    sleep 1
  done
  echo "agent-qa did not report expected SHA $expected" >&2
  return 1
}

start_sha() {
  local target="$1"
  AGENT_QA_IMAGE="$(image_for "$target")" AGENT_QA_SHA="$target" \
    docker compose -p agent-qa --project-directory "$APP_DIR" -f "$COMPOSE_FILE" \
      up -d --no-deps --force-recreate agent-qa
  wait_ready "$target"
}

case "$action" in
  deploy)
    [[ "$sha" =~ ^[a-f0-9]{40}$ ]] || { echo "Deploy requires a full 40-character SHA" >&2; exit 2; }
    archive="$(mktemp "$APP_DIR/.image.XXXXXXXX.tar")"
    trap 'rm -f "$archive"' EXIT
    cat > "$archive"
    size="$(stat -c '%s' "$archive")"
    (( size <= 209715200 )) || { echo "Image archive exceeds 200 MiB" >&2; exit 1; }
python3 - "$archive" "$(image_for "$sha")" <<'PY'
import json
import sys
import tarfile

archive_path, expected_tag = sys.argv[1:]
try:
    with tarfile.open(archive_path, "r:") as archive:
        manifest_file = archive.extractfile("manifest.json")
        if manifest_file is None:
            raise ValueError("missing image manifest")
        manifest = json.load(manifest_file)
except (OSError, tarfile.TarError, KeyError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(f"Invalid Docker image archive: {exc}") from exc
if (not isinstance(manifest, list) or len(manifest) != 1
        or not isinstance(manifest[0], dict)
        or manifest[0].get("RepoTags") != [expected_tag]):
    raise SystemExit("Image archive must contain only the requested agent-qa tag")
PY
    docker load --input "$archive"
    docker image inspect "$(image_for "$sha")" >/dev/null
    old_sha="$(ready_sha)"
    if [[ -z "$old_sha" && -f "$STATE_FILE" ]]; then old_sha="$(cat "$STATE_FILE")"; fi
    if ! start_sha "$sha"; then
      if valid_sha "$old_sha" && docker image inspect "$(image_for "$old_sha")" >/dev/null 2>&1; then
        echo "Deploy failed; restoring prior agent-qa image $old_sha" >&2
        start_sha "$old_sha" && printf '%s\n' "$old_sha" > "$STATE_FILE" || true
      fi
      exit 1
    fi
    if valid_sha "$old_sha" && [[ "$old_sha" != "$sha" ]]; then printf '%s\n' "$old_sha" > "$PREVIOUS_FILE"; fi
    printf '%s\n' "$sha" > "$STATE_FILE"
    printf 'AGENT_QA_READY=%s\n' "$(ready_json)"
    ;;
  rollback)
    [[ "$sha" =~ ^[a-f0-9]{40}$ ]] || { echo "Rollback requires the full 40-character SHA" >&2; exit 2; }
    docker image inspect "$(image_for "$sha")" >/dev/null
    old_sha="$(ready_sha)"
    start_sha "$sha"
    if valid_sha "$old_sha" && [[ "$old_sha" != "$sha" ]]; then printf '%s\n' "$old_sha" > "$PREVIOUS_FILE"; fi
    printf '%s\n' "$sha" > "$STATE_FILE"
    printf 'AGENT_QA_READY=%s\n' "$(ready_json)"
    ;;
  *)
    echo "Invalid operation" >&2
    exit 2
    ;;
esac
