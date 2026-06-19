#!/usr/bin/env bash
set -euo pipefail

workspace="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$workspace"

load_env_file() {
  local env_path="$1"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    if [[ -z "$line" || "${line:0:1}" == "#" || "$line" != *=* ]]; then
      continue
    fi

    local key="${line%%=*}"
    local value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    key="${key#"${key%%[![:space:]]*}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"

    if [[ ${#value} -ge 2 ]]; then
      if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
        value="${value:1:${#value}-2}"
      elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
        value="${value:1:${#value}-2}"
      fi
    fi

    if [[ -n "$key" && -z "${!key:-}" ]]; then
      export "$key=$value"
    fi
  done < "$env_path"
}

env_files=(".env" ".env.local" "db.env" "db.local.env")
for env_file in "${env_files[@]}"; do
  env_path="$workspace/$env_file"
  if [[ -f "$env_path" ]]; then
    load_env_file "$env_path"
  fi
done

required_vars=("DB_HOST" "DB_NAME" "DB_USER" "DB_PASSWORD")
missing_vars=()
for required_var in "${required_vars[@]}"; do
  if [[ -z "${!required_var:-}" ]]; then
    missing_vars+=("$required_var")
  fi
done

if [[ ${#missing_vars[@]} -gt 0 ]]; then
  printf 'Missing required environment variables: %s. Add them to .env or db.env in %s.\n' \
    "$(IFS=', '; echo "${missing_vars[*]}")" "$workspace" >&2
  exit 1
fi

if ! command -v flock >/dev/null 2>&1; then
  echo "Missing required command: flock" >&2
  exit 1
fi

mkdir -p "$workspace/logs"
timestamp="$(TZ="${TZ:-Europe/Warsaw}" date '+%Y-%m-%d_%H-%M-%S')"
log_path="$workspace/logs/ranking_snapshot_${timestamp}.log"
lock_path="$workspace/logs/ranking_snapshot.lock"

exec 9>"$lock_path"
if ! flock -n 9; then
  printf '[%s] Ranking snapshot refresh is already running. Lock: %s\n' \
    "$(TZ="${TZ:-Europe/Warsaw}" date '+%Y-%m-%dT%H:%M:%S')" "$lock_path" | tee -a "$log_path" >&2
  exit 1
fi

printf '[%s] Starting ranking snapshot refresh\n' \
  "$(TZ="${TZ:-Europe/Warsaw}" date '+%Y-%m-%dT%H:%M:%S')" | tee -a "$log_path"

if ! python generate_ranking_snapshot.py --refresh-source-view 2>&1 | tee -a "$log_path"; then
  printf '[%s] Ranking snapshot refresh failed. See %s\n' \
    "$(TZ="${TZ:-Europe/Warsaw}" date '+%Y-%m-%dT%H:%M:%S')" "$log_path" | tee -a "$log_path" >&2
  exit 1
fi

printf '[%s] Ranking snapshot refresh finished successfully\n' \
  "$(TZ="${TZ:-Europe/Warsaw}" date '+%Y-%m-%dT%H:%M:%S')" | tee -a "$log_path"
