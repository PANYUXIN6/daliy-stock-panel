#!/usr/bin/env bash
# Keep all relative mounts and environment interpolation rooted in the server runtime.
set -euo pipefail

if [[ $# -lt 2 || $1 != /* || ! -d $1 ]]; then
  echo "Usage: bash preproduction_compose.sh ABSOLUTE_RUNTIME_DIR COMPOSE_ARGS..." >&2
  exit 2
fi
runtime=$(cd -- "$1" && pwd -P)
shift
for file in .env tiers.yaml docker-compose.yml docker-compose.override.yml; do
  [[ -f "$runtime/$file" ]] || { echo "Missing runtime file: $file" >&2; exit 1; }
done

files=(-f "$runtime/docker-compose.yml")
if [[ -e "$runtime/current" || -L "$runtime/current" ]]; then
  [[ -L "$runtime/current" ]] || { echo "current must be a release symlink" >&2; exit 1; }
  release=$(cd -- "$runtime/current" && pwd -P)
  case "$release" in
    "$runtime/releases/"*) ;;
    *) echo "current points outside the release directory" >&2; exit 1 ;;
  esac
  [[ -f "$release/docker-compose.yml" && -f "$release/.deploy-image.yml" ]] || {
    echo "Incomplete current release" >&2; exit 1;
  }
  files=(-f "$release/docker-compose.yml")
fi
files+=(-f "$runtime/docker-compose.override.yml")
if [[ -n ${release:-} ]]; then
  files+=(-f "$release/.deploy-image.yml")
  if [[ -f "$release/.deploy-https.json" ]]; then
    files+=(-f "$release/docker-compose.https.yml" -f "$release/.deploy-https.json")
  fi
fi
exec docker compose --project-directory "$runtime" --env-file "$runtime/.env" \
  -p stock-panel "${files[@]}" "$@"
