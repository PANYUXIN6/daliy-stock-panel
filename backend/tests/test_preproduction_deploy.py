"""Exercise deployment transactions without Docker, SSH, or network access."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts" / "deploy_preproduction.sh"
MANAGE = ROOT / "scripts" / "preproduction_compose.sh"
SHA = "a" * 40
NEXT_SHA = "b" * 40
OLD_SHA = "d" * 40
OLD_WEB_IMAGE = "sha256:" + "1" * 64
OLD_COLLECTOR_IMAGE = "sha256:" + "2" * 64
NEW_IMAGE = "sha256:" + "3" * 64
CADDY_IMAGE = "sha256:" + "5" * 64
DOMAIN = "shuaihub.cn"

DOCKER = r'''
import json, os, pathlib, sys
import yaml

class ComposeLoader(yaml.SafeLoader):
    pass

ComposeLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))

state_path = pathlib.Path(os.environ["DEPLOY_TEST_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state["calls"].append(args)
state_path.write_text(json.dumps(state))

if args[0] == "compose":
    files = [args[i + 1] for i, value in enumerate(args) if value == "-f"]
    action = next(value for value in args if value in {"ps", "config", "up", "logs", "stop"})
    if action == "ps":
        if args[-1] in state["running"]:
            print(args[-1] + "-container")
    elif action == "config" and "--quiet" not in args:
        print(json.dumps({"services": {"web": {"build": {"args": state.get("build_args", {"BACKEND_EXTRAS": "backtest"})}}}}))
    elif action == "up":
        # Model service identity and rollback ordering, not Compose merge rules.
        services = {}
        for filename in files:
            for service, settings in yaml.load(pathlib.Path(filename).read_text(), Loader=ComposeLoader).get("services", {}).items():
                target = services.setdefault(service, {})
                target.update({key: value for key, value in settings.items() if key != "volumes"})
                for volume in settings.get("volumes", []):
                    target.setdefault("volumes", {})[volume["target"] if isinstance(volume, dict) else volume.split(":")[1]] = volume
        if "caddy" not in services and "caddy" in state["running"] and state["initial_web_binding"]["HostPort"] == "80":
            sys.exit("Caddy must release port 80 before restoring the legacy web container")
        for service, settings in services.items():
            if "image" in settings:
                state["running"][service] = state["images"].get(settings["image"], settings["image"])
        state["web_binding"] = state.get("https_web_binding", {"HostIp": "127.0.0.1", "HostPort": "3018"}) if "caddy" in services else state["initial_web_binding"]
        if "caddy" in services:
            state["caddy_settings"] = services["caddy"]
        state_path.write_text(json.dumps(state))
    elif action == "stop":
        state["running"].pop(args[-1], None)
        state_path.write_text(json.dumps(state))
elif args[0] == "pull":
    state["images"][args[-1]] = state["caddy_image"]
    state_path.write_text(json.dumps(state))
elif args[0] == "build":
    if state.get("fail_build"):
        sys.exit(17)
    state["images"][args[args.index("-t") + 1]] = state["new_image"]
    state_path.write_text(json.dumps(state))
elif args[0] == "top":
    if args[2:] != ["-eo", "pid,args", "-ww"]:
        sys.exit("Docker top requires a PID column and untruncated arguments")
    print("PID COMMAND")
    for index, command in enumerate(state.get("processes", ["uv run --no-sync uvicorn app.collector:app"]), 1):
        print(index, command)
elif args[:2] == ["image", "inspect"]:
    image = state["images"].get(args[-1])
    if image is None:
        sys.exit(1)
    print(image if "--format" in args else json.dumps([{"Id": image}]))
elif args[0] == "inspect":
    names = [name for name in args[1:] if name.endswith("-container")]
    if "--format" in args:
        print(state["running"][names[0].removesuffix("-container")])
    else:
        print(json.dumps([{
            "Image": state["running"][name.removesuffix("-container")],
            "State": {"Running": True, "Health": {"Status": "unhealthy" if name.startswith("caddy") and state.get("fail_caddy_health") and state["running"]["web"] == state["new_image"] else "healthy"}},
            "Config": {"Env": ["DATA_DIR=/app/data"]},
            "Mounts": [{"Source": state["data_dir"], "Destination": "/app/data", "Type": "bind"}],
            "NetworkSettings": {"Ports": {"3018/tcp": [state["web_binding"]] if name.startswith("web") else None}},
        } for name in names]))
else:
    sys.exit("Unexpected Docker operation")
'''

PYTHON = r'''
import http.client, io, json, os, pathlib, ssl, sys, time, urllib.request

if len(sys.argv) > 2 and sys.argv[1] == "-c" and "# stock-panel-deployment-health" in sys.argv[2]:
    code = sys.argv[2]
    def health_response(url, timeout):
        path = pathlib.Path(os.environ["DEPLOY_TEST_STATE"])
        state = json.loads(path.read_text())
        state.setdefault("health_urls", []).append(url)
        path.write_text(json.dumps(state))
        connected = not (state.get("fail_health") and state["running"]["web"] == state["new_image"])
        return io.StringIO(json.dumps({"status": "ok", "service_role": "web", "collector_connected": connected}))
    def tls_request(connection, method, url, *args, **kwargs):
        path = pathlib.Path(os.environ["DEPLOY_TEST_STATE"])
        state = json.loads(path.read_text())
        state.setdefault("tls_requests", []).append({
            "host": connection.host, "method": method, "path": url,
            "verify_mode": int(connection._context.verify_mode),
            "check_hostname": connection._context.check_hostname,
        })
        path.write_text(json.dumps(state))
        if state.get("fail_tls") and state["running"]["web"] == state["new_image"]:
            raise ssl.SSLCertVerificationError("simulated invalid certificate")
    def tls_response(connection):
        response = io.BytesIO(json.dumps({"status": "ok", "service_role": "web", "collector_connected": True}).encode())
        response.status = 200
        return response
    urllib.request.urlopen = health_response
    http.client.HTTPSConnection.request = tls_request
    http.client.HTTPSConnection.getresponse = tls_response
    time.sleep = lambda duration: None
    sys.argv = sys.argv[2:]
    exec(compile(code, "<deployment-health>", "exec"))
else:
    os.execv(REAL_PYTHON, [REAL_PYTHON, *sys.argv[1:]])
'''


def _archive(path: Path, *, unsafe: bool = False) -> Path:
    with tarfile.open(path, "w:gz") as output:
        for name, content in {
            "Dockerfile": "FROM scratch\n",
            "docker-compose.yml": "services:\n  web: {}\n  collector: {}\n",
            "docker-compose.https.yml": (ROOT / "docker-compose.https.yml").read_text(),
            "deploy/Caddyfile": (ROOT / "deploy/Caddyfile").read_text(),
            "tiers.yaml": "local configuration must not replace remote\n",
            ".env": "LOCAL_SECRET=must-not-migrate\n",
            "data/users/local.json": "local personal data must not migrate\n",
            **({"../escaped": "unsafe"} if unsafe else {}),
        }.items():
            body = content.encode()
            member = tarfile.TarInfo(name)
            member.size = len(body)
            output.addfile(member, io.BytesIO(body))
    return path


@pytest.fixture
def deployment(tmp_path):
    runtime = tmp_path / "remote runtime"
    runtime.mkdir()
    for name, content in {
        ".env": f"BACKEND_EXTRAS=backtest\nUNTRUSTED=$(touch '{runtime / 'should-not-exist'}')\n",
        "tiers.yaml": "remote tiers\n",
        "docker-compose.yml": "services:\n  web: {}\n  collector: {}\n",
        "docker-compose.override.yml": "services:\n  web:\n    image: stock-panel:initial\n  collector:\n    image: stock-panel:initial\n",
        ".deployed-commit": OLD_SHA + "\n",
        "data/auth/auth.sqlite3": "remote authentication\n",
        "data/users/admin/watchlist.json": "remote watchlist\n",
        "data/kline_daily/remote.parquet": "remote market data\n",
        "data/job_store/finished.json": '{"status":"succeeded"}',
    }.items():
        target = runtime / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    state = tmp_path / "docker-state.json"
    state.write_text(json.dumps({
        "calls": [],
        "running": {"web": OLD_WEB_IMAGE, "collector": OLD_COLLECTOR_IMAGE},
        "images": {},
        "new_image": NEW_IMAGE,
        "caddy_image": CADDY_IMAGE,
        "initial_web_binding": {"HostIp": "127.0.0.1", "HostPort": "18018"},
        "web_binding": {"HostIp": "127.0.0.1", "HostPort": "18018"},
        "data_dir": str(runtime / "data"),
    }))
    binary = tmp_path / "bin"
    binary.mkdir()
    for name, code in {
        "docker": DOCKER,
        "python3": "REAL_PYTHON = " + repr(sys.executable) + "\n" + PYTHON,
        "flock": "import os, sys\nsys.exit(int(os.environ.get('DEPLOY_TEST_LOCK_BUSY', '0')))\n",
    }.items():
        executable = binary / name
        executable.write_text(f"#!{sys.executable}\n" + code)
        executable.chmod(0o755)
    environment = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"], DEPLOY_TEST_STATE=str(state))
    archive = _archive(tmp_path / "source.tar.gz")
    return runtime, state, environment, archive


def _run(deployment, *, sha=SHA, archive=None, domain=None):
    runtime, _, environment, default_archive = deployment
    return subprocess.run(
        ["bash", str(DEPLOY), str(archive or default_archive), sha, str(runtime), *([domain] if domain is not None else [])],
        env=environment, text=True, capture_output=True, timeout=15, check=False,
    )


def test_deploy_preserves_remote_state_and_uses_stable_management_entry(deployment):
    runtime, state_file, environment, _ = deployment
    protected = {path: path.read_bytes() for path in runtime.rglob("*") if path.is_file()}
    result = _run(deployment)
    assert result.returncode == 0, result.stderr
    assert (runtime / "current").resolve() == runtime / "releases" / SHA
    assert (runtime / ".deployed-commit").read_text().strip() == SHA
    for path, content in protected.items():
        if path.name != ".deployed-commit":
            assert path.read_bytes() == content
    assert not (runtime / "data/users/local.json").exists()
    assert not (runtime / "should-not-exist").exists()
    state = json.loads(state_file.read_text())
    assert state["running"] == {"web": NEW_IMAGE, "collector": NEW_IMAGE}
    assert set(state["health_urls"]) == {"http://127.0.0.1:18018/health"}
    build, = [call for call in state["calls"] if call[0] == "build"]
    assert "BACKEND_EXTRAS=backtest" in build
    assert _run(deployment).returncode == 0  # A successful commit can be rerun.
    result = subprocess.run(
        ["bash", str(MANAGE), str(runtime), "logs", "web"],
        env=environment, text=True, capture_output=True, timeout=5, check=False,
    )
    assert result.returncode == 0, result.stderr
    call = json.loads(state_file.read_text())["calls"][-1]
    assert call[call.index("--project-directory") + 1] == str(runtime)
    assert call[call.index("--env-file") + 1] == str(runtime / ".env")
    assert str(runtime / "releases" / SHA / "docker-compose.yml") in call
    assert str(runtime / "docker-compose.override.yml") in call


def test_deploy_preserves_remote_plugin_mirror_and_optional_build_arguments(deployment):
    runtime, state_file, _, _ = deployment
    state = json.loads(state_file.read_text())
    arguments = {
        "BACKEND_EXTRAS": "backtest legacy-cpu",
        "INCLUDE_STOCKSDK": "1",
        "USE_CN_MIRROR": "1",
        "NPM_REGISTRY": "https://registry.example.test/",
        "PYPI_INDEX": "https://packages.example.test/simple",
        "OPTIONAL_FROM_ENV": None,
        "CUSTOM_VALUE": f"literal $(touch '{runtime / 'must-not-execute'}')\nsecond line",
    }
    state["build_args"] = arguments
    state_file.write_text(json.dumps(state))
    result = _run(deployment)
    assert result.returncode == 0, result.stderr
    state = json.loads(state_file.read_text())
    build, = [call for call in state["calls"] if call[0] == "build"]
    actual = [build[index + 1] for index, value in enumerate(build) if value == "--build-arg"]
    assert actual == [key if value is None else f"{key}={value}" for key, value in arguments.items()]
    assert not (runtime / "must-not-execute").exists()
    assert state["running"] == {"web": NEW_IMAGE, "collector": NEW_IMAGE}


@pytest.mark.parametrize("failure", ["fail_build", "fail_health"])
def test_failed_deploy_keeps_previous_version_and_recovers_both_images(deployment, failure):
    runtime, state_file, _, _ = deployment
    state = json.loads(state_file.read_text())
    state[failure] = True
    state["build_args"] = {"PRIVATE_BUILD_OPTION": "build-only-sensitive-test-value"}
    state_file.write_text(json.dumps(state))
    result = _run(deployment)
    assert result.returncode != 0
    assert "build-only-sensitive-test-value" not in result.stdout + result.stderr
    state = json.loads(state_file.read_text())
    assert state["running"] == {"web": OLD_WEB_IMAGE, "collector": OLD_COLLECTOR_IMAGE}
    assert not (runtime / "current").exists()
    assert (runtime / ".deployed-commit").read_text().strip() == OLD_SHA
    up = [call for call in state["calls"] if "up" in call]
    assert len(up) == (0 if failure == "fail_build" else 2)
    if failure == "fail_health":
        assert "Previous deployment restored" in result.stderr


def test_rollback_uses_last_successful_release_compose(deployment):
    runtime, state_file, _, _ = deployment
    assert _run(deployment).returncode == 0
    state = json.loads(state_file.read_text())
    state.update(fail_health=True, new_image="sha256:" + "4" * 64)
    state_file.write_text(json.dumps(state))
    result = _run(deployment, sha=NEXT_SHA)
    assert result.returncode != 0
    assert (runtime / "current").resolve() == runtime / "releases" / SHA
    assert (runtime / ".deployed-commit").read_text().strip() == SHA
    state = json.loads(state_file.read_text())
    assert state["running"] == {"web": NEW_IMAGE, "collector": NEW_IMAGE}
    rollback = [call for call in state["calls"] if "up" in call][-1]
    assert str(runtime / "releases" / SHA / "docker-compose.yml") in rollback
    assert str(runtime / "releases" / NEXT_SHA / "docker-compose.yml") not in rollback


def test_https_deploy_pins_caddy_preserves_remote_state_and_reuses_certificate_volumes(deployment):
    runtime, state_file, environment, _ = deployment
    protected = {path: path.read_bytes() for path in runtime.rglob("*") if path.is_file()}
    state = json.loads(state_file.read_text())
    public_binding = {"HostIp": "0.0.0.0", "HostPort": "80"}
    state.update(initial_web_binding=public_binding, web_binding=public_binding)
    state_file.write_text(json.dumps(state))

    result = _run(deployment, domain=DOMAIN)

    assert result.returncode == 0, result.stderr
    release = runtime / "releases" / SHA
    https_file = release / ".deploy-https.json"
    https = json.loads(https_file.read_text())
    caddy = https["services"]["caddy"]
    assert caddy["image"] == CADDY_IMAGE
    assert caddy["environment"]["SITE_DOMAIN"] == DOMAIN
    mount, = [volume for volume in caddy["volumes"] if volume["target"] == "/etc/caddy/Caddyfile"]
    assert mount["source"] == str(release / "deploy/Caddyfile")
    assert mount["read_only"] is True
    state = json.loads(state_file.read_text())
    assert state["running"] == {"web": NEW_IMAGE, "collector": NEW_IMAGE, "caddy": CADDY_IMAGE}
    assert state["web_binding"] == {"HostIp": "127.0.0.1", "HostPort": "3018"}
    assert state["tls_requests"][-1] == {
        "host": DOMAIN, "method": "GET", "path": "/health",
        "verify_mode": 2, "check_hostname": True,
    }
    assert state["caddy_settings"]["volumes"]["/data"] == "caddy_data:/data"
    assert state["caddy_settings"]["volumes"]["/config"] == "caddy_config:/config"
    state["images"]["caddy:2-alpine"] = "sha256:" + "6" * 64
    state_file.write_text(json.dumps(state))
    assert _run(deployment, domain=DOMAIN).returncode == 0
    assert json.loads(https_file.read_text()) == https
    state = json.loads(state_file.read_text())
    assert state["running"]["caddy"] == CADDY_IMAGE
    assert [call for call in state["calls"] if call[0] == "pull"] == [["pull", "caddy:2-alpine"]]
    for path, content in protected.items():
        if path.name != ".deployed-commit":
            assert path.read_bytes() == content
    assert not (runtime / "data/users/local.json").exists()
    for call in state["calls"]:
        if call[0] == "compose":
            assert call[call.index("-p") + 1] == "stock-panel"
            assert "down" not in call and "--volumes" not in call

    result = subprocess.run(
        ["bash", str(MANAGE), str(runtime), "logs", "caddy"],
        env=environment, text=True, capture_output=True, timeout=5, check=False,
    )
    assert result.returncode == 0, result.stderr
    call = json.loads(state_file.read_text())["calls"][-1]
    files = [call[index + 1] for index, argument in enumerate(call) if argument == "-f"]
    assert files == [
        str(release / "docker-compose.yml"), str(runtime / "docker-compose.override.yml"),
        str(release / ".deploy-image.yml"), str(release / "docker-compose.https.yml"), str(https_file),
    ]


@pytest.mark.parametrize("failure", ["fail_tls", "fail_caddy_health"])
def test_https_failure_releases_caddy_before_restoring_legacy_public_http(deployment, failure):
    runtime, state_file, _, _ = deployment
    state = json.loads(state_file.read_text())
    binding = {"HostIp": "0.0.0.0", "HostPort": "80"}
    state.update(initial_web_binding=binding, web_binding=binding)
    state[failure] = True
    state_file.write_text(json.dumps(state))

    result = _run(deployment, domain=DOMAIN)

    assert result.returncode != 0
    assert "Previous deployment restored" in result.stderr
    assert "ROLLBACK FAILED" not in result.stderr
    assert not (runtime / "current").exists()
    assert (runtime / ".deployed-commit").read_text().strip() == OLD_SHA
    state = json.loads(state_file.read_text())
    assert state["running"] == {"web": OLD_WEB_IMAGE, "collector": OLD_COLLECTOR_IMAGE}
    assert state["web_binding"] == binding
    transitions = [call for call in state["calls"] if call[0] == "compose" and any(action in call for action in ("up", "stop"))]
    assert len(transitions) == 3
    assert "up" in transitions[0]
    assert transitions[1][-2:] == ["stop", "caddy"]
    assert str(runtime / "releases" / SHA / ".deploy-https.json") in transitions[1]
    assert "up" in transitions[2] and str(runtime / "docker-compose.yml") in transitions[2]
    assert "http://127.0.0.1:80/health" in state["health_urls"]


def test_failed_https_update_restores_previous_caddy_image_and_release_configuration(deployment):
    runtime, state_file, _, _ = deployment
    assert _run(deployment, domain=DOMAIN).returncode == 0
    old_release = runtime / "releases" / SHA
    previous_https = (old_release / ".deploy-https.json").read_bytes()
    state = json.loads(state_file.read_text())
    previous_caddy_image = "sha256:" + "7" * 64
    state["running"]["caddy"] = previous_caddy_image
    state.update(new_image="sha256:" + "4" * 64, caddy_image="sha256:" + "6" * 64, fail_tls=True)
    state_file.write_text(json.dumps(state))

    # An existing HTTPS installation keeps HTTPS even if the runner omits the URL.
    result = _run(deployment, sha=NEXT_SHA)

    assert result.returncode != 0
    assert "Previous deployment restored" in result.stderr
    assert (runtime / "current").resolve() == old_release
    assert (runtime / ".deployed-commit").read_text().strip() == SHA
    assert (old_release / ".deploy-https.json").read_bytes() == previous_https
    state = json.loads(state_file.read_text())
    assert state["running"] == {"web": NEW_IMAGE, "collector": NEW_IMAGE, "caddy": previous_caddy_image}
    assert state["caddy_settings"]["volumes"]["/etc/caddy/Caddyfile"]["source"] == str(old_release / "deploy/Caddyfile")
    assert state["caddy_settings"]["volumes"]["/data"] == "caddy_data:/data"
    rollback = [call for call in state["calls"] if "up" in call][-1]
    assert str(old_release / ".deploy-https.json") in rollback
    assert str(runtime / "releases" / NEXT_SHA / ".deploy-https.json") not in rollback
    assert state["tls_requests"][-1]["host"] == DOMAIN
    assert state["tls_requests"][-1]["check_hostname"] is True


@pytest.mark.parametrize("kind", ["pipeline", "mining"])
def test_active_tasks_abort_before_replacing_containers(deployment, kind):
    runtime, state_file, _, _ = deployment
    relative = "job_store/active.json" if kind == "pipeline" else "users/admin/research/mining/runs/active/manifest.json"
    target = runtime / "data" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"status": "running" if kind == "pipeline" else "cancelling"}))
    result = _run(deployment)
    assert result.returncode != 0
    assert "task status prevents deployment" in result.stderr
    assert not any("up" in call for call in json.loads(state_file.read_text())["calls"])


@pytest.mark.parametrize("worker", [False, True], ids=["resource-tracker-only", "spawned-worker"])
def test_spawned_computation_blocks_deploy_but_idle_resource_tracker_does_not(deployment, worker):
    runtime, state_file, _, _ = deployment
    state = json.loads(state_file.read_text())
    state["processes"] = [
        "uv run --no-sync uvicorn app.collector:app",
        "/app/.venv/bin/python -c from multiprocessing.resource_tracker import main;main(6)",
    ]
    if worker:
        state["processes"].append(
            "/app/.venv/bin/python -c from multiprocessing.spawn import spawn_main; "
            "spawn_main(tracker_fd=7, pipe_handle=23) --multiprocessing-fork"
        )
    state_file.write_text(json.dumps(state))
    result = _run(deployment)
    state = json.loads(state_file.read_text())
    assert ["top", "collector-container", "-eo", "pid,args", "-ww"] in state["calls"]
    if worker:
        assert result.returncode != 0
        assert "Active spawned computation worker prevents deployment" in result.stderr
        assert not any("up" in call for call in state["calls"])
        assert (runtime / ".deployed-commit").read_text().strip() == OLD_SHA
    else:
        assert result.returncode == 0, result.stderr
        assert state["running"] == {"web": NEW_IMAGE, "collector": NEW_IMAGE}


@pytest.mark.parametrize("invalid", ["sha", "runtime", "archive", "lock"])
def test_invalid_target_archive_or_busy_lock_never_switches_services(deployment, invalid):
    runtime, state_file, environment, archive = deployment
    if invalid == "runtime":
        (runtime / ".env").unlink()
    elif invalid == "archive":
        archive = _archive(archive, unsafe=True)
    elif invalid == "lock":
        environment["DEPLOY_TEST_LOCK_BUSY"] = "1"
    result = _run(deployment, sha="../unsafe" if invalid == "sha" else SHA, archive=archive)
    assert result.returncode != 0
    assert not (runtime.parent / "escaped").exists()
    assert not any("up" in call for call in json.loads(state_file.read_text())["calls"])
