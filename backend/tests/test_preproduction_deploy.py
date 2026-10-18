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

DOCKER = r'''
import json, os, pathlib, sys
import yaml

state_path = pathlib.Path(os.environ["DEPLOY_TEST_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state["calls"].append(args)
state_path.write_text(json.dumps(state))

if args[0] == "compose":
    files = [args[i + 1] for i, value in enumerate(args) if value == "-f"]
    action = next(value for value in args if value in {"ps", "config", "up", "logs"})
    if action == "ps":
        print(args[-1] + "-container")
    elif action == "config" and "--quiet" not in args:
        print(json.dumps({"services": {"web": {"build": {"args": state.get("build_args", {"BACKEND_EXTRAS": "backtest"})}}}}))
    elif action == "up":
        for filename in files:
            for service, settings in yaml.safe_load(pathlib.Path(filename).read_text()).get("services", {}).items():
                if "image" in settings:
                    state["running"][service] = state["images"].get(settings["image"], settings["image"])
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
            "State": {"Running": True, "Health": {"Status": "healthy"}},
            "Config": {"Env": ["DATA_DIR=/app/data"]},
            "Mounts": [{"Source": state["data_dir"], "Destination": "/app/data", "Type": "bind"}],
            "NetworkSettings": {"Ports": {"3018/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18018"}] if name.startswith("web") else None}},
        } for name in names]))
else:
    sys.exit("Unexpected Docker operation")
'''

PYTHON = r'''
import io, json, os, pathlib, sys, time, urllib.request

if len(sys.argv) > 2 and sys.argv[1] == "-c" and "# stock-panel-deployment-health" in sys.argv[2]:
    code = sys.argv[2]
    def health_response(url, timeout):
        path = pathlib.Path(os.environ["DEPLOY_TEST_STATE"])
        state = json.loads(path.read_text())
        state.setdefault("health_urls", []).append(url)
        path.write_text(json.dumps(state))
        connected = not (state.get("fail_health") and state["running"]["web"] == state["new_image"])
        return io.StringIO(json.dumps({"status": "ok", "service_role": "web", "collector_connected": connected}))
    urllib.request.urlopen = health_response
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


def _run(deployment, *, sha=SHA, archive=None):
    runtime, _, environment, default_archive = deployment
    return subprocess.run(
        ["bash", str(DEPLOY), str(archive or default_archive), sha, str(runtime)],
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
        "BACKEND_EXTRAS": "backtest desktop",
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
