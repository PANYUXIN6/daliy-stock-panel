#!/usr/bin/env bash
# Install code only. Runtime configuration and data always stay in RUNTIME_DIR.
set -Eeuo pipefail
umask 077

die() { echo "$*" >&2; exit 1; }
if [[ $# -ne 3 || ! $2 =~ ^[0-9a-f]{40}$ || $3 != /* || ! -d $3 ]]; then
  die "Usage: bash deploy_preproduction.sh ARCHIVE FULL_LOWERCASE_COMMIT_SHA ABSOLUTE_RUNTIME_DIR"
fi
archive=$1
sha=$2
runtime=$(cd -- "$3" && pwd -P)
[[ $runtime != / && -f $archive ]] || die "Invalid runtime directory or source archive"
for file in .env tiers.yaml docker-compose.yml docker-compose.override.yml; do
  [[ -f "$runtime/$file" ]] || die "Missing runtime file: $file"
done
command -v flock >/dev/null || die "flock is required on the deployment server"
exec 9>"$runtime/.deployment.lock"
flock -n 9 || die "Another deployment is running"

releases="$runtime/releases"
[[ ! -L $releases ]] || die "The releases directory must not be a symlink"
mkdir -p -- "$releases"
release="$releases/$sha"
old_release=
old_files=(-f "$runtime/docker-compose.yml")
if [[ -e "$runtime/current" || -L "$runtime/current" ]]; then
  [[ -L "$runtime/current" ]] || die "current must be a release symlink"
  old_release=$(cd -- "$runtime/current" && pwd -P)
  case "$old_release" in
    "$releases/"*) ;;
    *) die "current points outside the release directory" ;;
  esac
  [[ -f "$old_release/docker-compose.yml" && -f "$old_release/.deploy-image.yml" ]] \
    || die "Incomplete current release"
  old_files=(-f "$old_release/docker-compose.yml")
fi
old_files+=(-f "$runtime/docker-compose.override.yml")
[[ -z $old_release ]] || old_files+=(-f "$old_release/.deploy-image.yml")
compose=(docker compose --project-directory "$runtime" --env-file "$runtime/.env" -p stock-panel)
old_web=$("${compose[@]}" "${old_files[@]}" ps -q web)
old_collector=$("${compose[@]}" "${old_files[@]}" ps -q collector)
[[ -n $old_web && -n $old_collector && $old_web != *$'\n'* && $old_collector != *$'\n'* ]] \
  || die "Exactly one existing web and collector container is required"
old_web_image=$(docker inspect --format '{{.Image}}' "$old_web")
old_collector_image=$(docker inspect --format '{{.Image}}' "$old_collector")
[[ $old_web_image =~ ^sha256:[0-9a-f]{64}$ && $old_collector_image =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "Cannot identify the existing container images"
old_commit=
if [[ -e "$runtime/.deployed-commit" ]]; then
  old_commit=$(cat "$runtime/.deployed-commit")
  [[ $old_commit =~ ^[0-9a-f]{40}$ ]] || die "Invalid existing deployment commit marker"
fi

stage=
rollback_file=$(mktemp "$runtime/.deploy-rollback.XXXXXX")
rollback_armed=0
cat >"$rollback_file" <<YAML
services:
  web:
    image: $old_web_image
  collector:
    image: $old_collector_image
YAML

health() {
  local expected_web=$1 expected_collector=$2 web collector
  shift 2
  web=$("${compose[@]}" "$@" ps -q web)
  collector=$("${compose[@]}" "$@" ps -q collector)
  docker inspect "$web" "$collector" | python3 -c '
# stock-panel-deployment-health
import json, sys, time, urllib.error, urllib.request
web, collector = json.load(sys.stdin)
for container in (web, collector):
    if not container["State"]["Running"] or container["State"].get("Health", {}).get("Status") != "healthy":
        sys.exit("A deployment container is not healthy")
if any(collector["NetworkSettings"]["Ports"].values()):
    sys.exit("Collector must not publish host ports")
if web["Image"] != sys.argv[1] or collector["Image"] != sys.argv[2]:
    sys.exit("Deployment containers do not use the expected images")
bindings = web["NetworkSettings"]["Ports"].get("3018/tcp") or []
if len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
    sys.exit("Web must publish exactly one loopback HTTP port")
url = "http://127.0.0.1:" + str(int(bindings[0]["HostPort"])) + "/health"
for attempt in range(30):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            status = json.load(response)
        if status.get("status") == "ok" and status.get("service_role") == "web" and status.get("collector_connected") is True:
            sys.exit(0)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        pass
    if attempt < 29:
        time.sleep(2)
sys.exit("Web failed to reconnect to collector")
' "$expected_web" "$expected_collector"
}

finish() {
  local result=$?
  trap - EXIT INT TERM
  if [[ $result -ne 0 && $rollback_armed == 1 ]]; then
    echo "Deployment failed; restoring previous web and collector images" >&2
    if "${compose[@]}" "${old_files[@]}" -f "$rollback_file" up -d --no-build --wait --wait-timeout 240 \
      && health "$old_web_image" "$old_collector_image" "${old_files[@]}" -f "$rollback_file"; then
      echo "Previous deployment restored" >&2
    else
      echo "ROLLBACK FAILED: inspect the server before retrying" >&2
    fi
    python3 - "$runtime" "$old_release" "$old_commit" <<'PY'
import os
import pathlib
import sys
import tempfile

runtime, release, sha = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
if release:
    link = runtime / (".current.rollback." + str(os.getpid()))
    link.symlink_to(release, target_is_directory=True)
    os.replace(link, runtime / "current")
else:
    (runtime / "current").unlink(missing_ok=True)
if sha:
    descriptor, name = tempfile.mkstemp(prefix=".deployed-commit.", dir=runtime)
    with os.fdopen(descriptor, "w") as output:
        output.write(sha + "\n")
    os.replace(name, runtime / ".deployed-commit")
else:
    (runtime / ".deployed-commit").unlink(missing_ok=True)
PY
  fi
  [[ -z $stage || ! -d $stage ]] || rm -rf -- "$stage"
  rm -f -- "$rollback_file"
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -e $release || -L $release ]]; then
  [[ ! -L $release && -d $release && -f "$release/.release-commit" ]] \
    || die "Existing release is not a complete managed release"
  [[ $(cat "$release/.release-commit") == "$sha" ]] || die "Release identity mismatch"
else
  stage=$(mktemp -d "$releases/.staging-$sha.XXXXXX")
  python3 - "$archive" "$stage" <<'PY'
import pathlib
import sys
import tarfile

with tarfile.open(sys.argv[1], "r:gz") as source:
    members = source.getmembers()
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
            sys.exit("Source archive contains unsafe paths or links")
    source.extractall(sys.argv[2], members=members, filter="data")
PY
  [[ -f "$stage/Dockerfile" && -f "$stage/docker-compose.yml" ]] || die "Source archive lacks deployment files"
  printf '%s\n' "$sha" >"$stage/.release-commit"
  mv -- "$stage" "$release"
  stage=
fi

image="stock-panel:$sha"
if ! docker image inspect "$image" >/dev/null 2>&1; then
  # Preserve resolved server build arguments without executing .env or shell-expanding values.
  "${compose[@]}" "${old_files[@]}" config --format json | python3 -c '
import json, subprocess, sys

config = json.load(sys.stdin)
arguments = config["services"]["web"].get("build", {}).get("args", {})
if not isinstance(arguments, dict) or any(
    not isinstance(key, str) or (value is not None and not isinstance(value, str))
    for key, value in arguments.items()
):
    sys.exit("Invalid server build arguments")
command = ["docker", "build"]
for key, value in arguments.items():
    command.extend(["--build-arg", key if value is None else key + "=" + value])
command.extend(["-t", sys.argv[1], sys.argv[2]])
# Do not include argument values in a CalledProcessError traceback on failure.
sys.exit(subprocess.run(command, check=False).returncode)
' "$image" "$release"
fi
new_image=$(docker image inspect --format '{{.Id}}' "$image")
[[ $new_image =~ ^sha256:[0-9a-f]{64}$ ]] || die "Cannot identify the built image"
cat >"$release/.deploy-image.yml" <<YAML
services:
  web:
    image: $new_image
  collector:
    image: $new_image
YAML
new_files=(-f "$release/docker-compose.yml" -f "$runtime/docker-compose.override.yml" -f "$release/.deploy-image.yml")
"${compose[@]}" "${new_files[@]}" config --quiet

# Inspect only persisted status fields; importing application stores would mutate them.
docker inspect "$old_collector" | python3 -c '
import json, pathlib, sys

container, = json.load(sys.stdin)
environment = dict(item.split("=", 1) for item in container["Config"]["Env"] if "=" in item)
if environment.get("DATA_DIR") != "/app/data":
    sys.exit("Cannot locate the current collector data directory safely")
mounts = [item for item in container["Mounts"] if item["Destination"] == "/app/data" and item["Type"] == "bind"]
if len(mounts) != 1:
    sys.exit("Cannot locate the current collector data mount safely")
root = pathlib.Path(mounts[0]["Source"]).resolve(strict=True)
groups = [
    (root.glob("job_store/*.json"), {"succeeded", "failed"}),
    (root.glob("users/*/research/mining/runs/*/manifest.json"), {
        "succeeded", "succeeded_with_budget_exhausted", "failed", "cancelled", "interrupted", "skipped_prerequisite"
    }),
]
for paths, terminal in groups:
    for path in paths:
        if not path.resolve().is_relative_to(root):
            sys.exit("Task state path escapes the collector data directory")
        try:
            with path.open(encoding="utf-8") as source:
                record = json.load(source)
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError):
            sys.exit("Cannot read task status safely; deployment aborted")
        if not isinstance(record, dict) or not isinstance(record.get("status"), str) or record["status"] not in terminal:
            sys.exit("Active or unknown task status prevents deployment; retry after tasks finish")
'
# Backtest/optimization workers are process-local jobs without persisted status files.
docker top "$old_collector" -eo pid,args -ww | python3 -c '
import re, sys

for command in sys.stdin:
    if "multiprocessing.spawn" in command and re.search(r"\bspawn_main\s*\(", command):
        sys.exit("Active spawned computation worker prevents deployment; retry after tasks finish")
'
rollback_armed=1
"${compose[@]}" "${new_files[@]}" up -d --no-build --wait --wait-timeout 240
health "$new_image" "$new_image" "${new_files[@]}"

# current is the authoritative management entry; replace it only after health succeeds.
python3 - "$runtime" "$release" "$sha" <<'PY'
import os
import pathlib
import sys
import tempfile

runtime, release, sha = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
descriptor, name = tempfile.mkstemp(prefix=".deployed-commit.", dir=runtime)
with os.fdopen(descriptor, "w") as output:
    output.write(sha + "\n")
link = runtime / (".current." + str(os.getpid()))
try:
    link.symlink_to(release, target_is_directory=True)
    os.replace(link, runtime / "current")
    os.replace(name, runtime / ".deployed-commit")
finally:
    link.unlink(missing_ok=True)
    pathlib.Path(name).unlink(missing_ok=True)
PY
rollback_armed=0
echo "Deployment healthy: $sha"
