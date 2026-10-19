#!/usr/bin/env bash
# GitHub runner 入口；仅打包 Git 内代码，不上传本机运行数据或配置。
set -Eeuo pipefail

die() { printf '%s\n' "$*" >&2; exit 1; }

[[ "${GITHUB_EVENT_NAME:-}" == push && "${GITHUB_REF:-}" == refs/heads/main ]] || {
    echo 'Deployment is only permitted for a push to main.' >&2
    exit 1
}
[[ "${GITHUB_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || die 'Invalid GitHub commit SHA.'
[[ "${DEPLOY_HOST:-}" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || die 'DEPLOY_HOST is missing or invalid.'
[[ "${DEPLOY_USER:-}" =~ ^[a-z_][a-z0-9_-]*$ ]] || die 'DEPLOY_USER is missing or invalid.'
[[ "${DEPLOY_PORT:-}" =~ ^[0-9]{1,5}$ ]] && (( 10#$DEPLOY_PORT > 0 && 10#$DEPLOY_PORT < 65536 )) \
    || die 'DEPLOY_PORT must be between 1 and 65535.'
[[ "${DEPLOY_DIR:-}" =~ ^/[a-zA-Z0-9_/-]+$ && "$DEPLOY_DIR" != / ]] || die 'DEPLOY_DIR must be an absolute deployment path.'
[[ -n "${DEPLOY_SSH_KEY:-}" && -n "${DEPLOY_KNOWN_HOSTS:-}" ]] || die 'Deployment SSH secrets are missing.'
domain=$(python3 - "${DEPLOY_URL:-}" <<'PY'
import re
import sys
from urllib.parse import urlsplit

value = sys.argv[1]
if not value:
    sys.exit(0)
try:
    url = urlsplit(value)
    host = url.hostname or ""
    labels = host.split(".")
    valid = (
        value.startswith("https://")
        and not any(ord(char) <= 32 for char in value)
        and not any(char in value for char in "?#\\")
        and url.username is None and url.password is None
        and url.port in (None, 443) and url.path in ("", "/")
        and len(host) <= 253 and len(labels) >= 2
        and any(char.isalpha() for char in labels[-1])
        and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
    )
except ValueError:
    valid = False
if not valid:
    sys.exit("DEPLOY_URL must be a root HTTPS domain URL on port 443.")
print(host)
PY
)

# 旧检查重跑不得把服务器回退到 main 之外的历史提交。
latest=$(gh api "repos/$GITHUB_REPOSITORY/git/ref/heads/main" --jq '.object.sha')
[[ "$latest" =~ ^[0-9a-f]{40}$ ]] || die 'Unable to resolve the current main commit.'
if [[ "$latest" != "$GITHUB_SHA" ]]; then
    echo 'A newer main commit exists; skipping this superseded deployment.'
    exit 0
fi

umask 077
work_dir=$(mktemp -d)
remote_dir=''
destination="$DEPLOY_USER@$DEPLOY_HOST"
ssh_options=(-i "$work_dir/key" -p "$DEPLOY_PORT" -o BatchMode=yes
    -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes
    -o "UserKnownHostsFile=$work_dir/known_hosts" -o ConnectTimeout=15
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
cleanup() {
    if [[ "$remote_dir" =~ ^/tmp/stock-panel-ci\.[a-zA-Z0-9]+$ ]]; then
        ssh "${ssh_options[@]}" "$destination" "rm -rf -- '$remote_dir'" >/dev/null 2>&1 || true
    fi
    rm -rf -- "$work_dir"
}
trap cleanup EXIT
printf '%s\n' "$DEPLOY_SSH_KEY" > "$work_dir/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$work_dir/known_hosts"
unset DEPLOY_SSH_KEY DEPLOY_KNOWN_HOSTS

git archive --format=tar.gz --output="$work_dir/release.tar.gz" "$GITHUB_SHA"
remote_dir=$(ssh "${ssh_options[@]}" "$destination" 'mktemp -d /tmp/stock-panel-ci.XXXXXXXX')
[[ "$remote_dir" =~ ^/tmp/stock-panel-ci\.[a-zA-Z0-9]+$ ]]
scp -i "$work_dir/key" -P "$DEPLOY_PORT" -o BatchMode=yes -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$work_dir/known_hosts" \
    "$work_dir/release.tar.gz" scripts/deploy_preproduction.sh "$destination:$remote_dir/"
remote_command="bash '$remote_dir/deploy_preproduction.sh' '$remote_dir/release.tar.gz' '$GITHUB_SHA' '$DEPLOY_DIR'"
[[ -z $domain ]] || remote_command+=" '$domain'"
ssh "${ssh_options[@]}" "$destination" "$remote_command"

if [[ -n "${DEPLOY_URL:-}" ]]; then
    curl --fail --silent --show-error --retry 3 --max-time 20 "${DEPLOY_URL%/}/health" |
        python3 -c 'import json,sys; r=json.load(sys.stdin); assert r["status"]=="ok" and r["service_role"]=="web" and r["collector_connected"] is True'
fi
printf 'Deployed main commit %s successfully.\n' "$GITHUB_SHA"
