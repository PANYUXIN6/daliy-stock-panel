"""Exercise runner deployment boundaries without contacting a server."""

import json
import os
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[2] / "scripts" / "ci_deploy.sh"
REMOTE_TEMP_DIR = "/tmp/stock-panel-ci.Test1234"

TRANSPORT_FAKE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import stat
    import sys
    import tarfile
    from pathlib import Path

    tool = Path(sys.argv[0]).name
    args = sys.argv[1:]
    call = {"tool": tool, "args": args}
    if tool in {"ssh", "scp"}:
        key = Path(args[args.index("-i") + 1])
        hosts = Path(next(arg.split("=", 1)[1] for arg in args
                          if arg.startswith("UserKnownHostsFile=")))
        call["credential_modes"] = [stat.S_IMODE(path.stat().st_mode)
                                    for path in (key, hosts)]
        call["credential_env_present"] = any(name in os.environ for name in
                                             ("DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS"))
    if tool == "scp":
        sources = [Path(arg) for arg in args[-3:-1]]
        call["uploaded_files"] = [path.name for path in sources]
        with tarfile.open(sources[0]) as archive:
            call["archive_files"] = {
                member.name: archive.extractfile(member).read().decode()
                for member in archive.getmembers() if member.isfile()
            }
    with Path(os.environ["DEPLOY_TEST_CALLS"]).open("a") as log:
        log.write(json.dumps(call) + "\\n")

    failure = os.environ.get("DEPLOY_TEST_FAILURE")
    if tool == "gh":
        print(os.environ.get("DEPLOY_TEST_MAIN_SHA", os.environ["GITHUB_SHA"]))
    elif tool == "ssh" and args[-1].startswith("mktemp "):
        print("/tmp/stock-panel-ci.Test1234")
    elif (tool == "scp" and failure == "upload") or (
        tool == "ssh" and args[-1].startswith("bash ") and failure == "remote"
    ):
        print("simulated deployment failure", file=sys.stderr)
        sys.exit(23)
    elif tool == "curl":
        print(json.dumps({"status": "ok", "service_role": "web",
                          "collector_connected": True}))
    """
)


@dataclass
class DeployRunner:
    repository: Path
    temporary: Path
    log: Path
    env: dict[str, str]

    def run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(RUNNER)],
            cwd=self.repository,
            env={**self.env, **overrides},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def remaining_work_directories(self) -> list[Path]:
        # macOS developer tools may add their own xcrun_db file to TMPDIR.
        return [path for path in self.temporary.iterdir() if path.is_dir()]


@pytest.fixture
def deploy_runner(tmp_path: Path) -> DeployRunner:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "scripts").mkdir()
    (repository / "README.md").write_text("A deployable test project.\n")
    (repository / "app.txt").write_text("tracked application release\n")
    (repository / "scripts" / "deploy_preproduction.sh").write_text("#!/bin/bash\nexit 0\n")
    for command in (
        ["git", "init", "--initial-branch=main"],
        ["git", "add", "."],
        ["git", "-c", "user.name=Deploy Test", "-c", "user.email=deploy@example.test",
         "commit", "-m", "initial fixture"],
    ):
        subprocess.run(command, cwd=repository, capture_output=True, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    (repository / ".env").write_text("private fixture configuration\n")
    (repository / "data").mkdir()
    (repository / "data" / "auth.sqlite3").write_text("private fixture user data\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    transport = fake_bin / "transport.py"
    transport.write_text(TRANSPORT_FAKE)
    transport.chmod(0o700)
    for name in ("gh", "ssh", "scp", "curl"):
        (fake_bin / name).symlink_to(transport)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    log = tmp_path / "calls.jsonl"
    log.touch()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(temporary),
        "DEPLOY_TEST_CALLS": str(log),
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": sha,
        "GITHUB_REPOSITORY": "example/test-project",
        "DEPLOY_HOST": "release.example.test",
        "DEPLOY_USER": "deployer",
        "DEPLOY_PORT": "22",
        "DEPLOY_DIR": "/opt/test-project",
        "DEPLOY_URL": "https://release.example.test",
        "DEPLOY_SSH_KEY": "fake-private-key-for-deploy-test",
        "DEPLOY_KNOWN_HOSTS": "release.example.test ssh-ed25519 fake-public-host-key",
    }
    return DeployRunner(repository, temporary, log, env)


@pytest.mark.parametrize(
    ("event", "ref"),
    [("pull_request", "refs/heads/main"), ("push", "refs/heads/develop")],
)
def test_rejects_untrusted_trigger_before_any_remote_access(deploy_runner, event, ref):
    result = deploy_runner.run(GITHUB_EVENT_NAME=event, GITHUB_REF=ref)

    assert result.returncode != 0
    assert deploy_runner.calls() == []
    assert deploy_runner.remaining_work_directories() == []


def test_superseded_main_commit_does_not_touch_server(deploy_runner):
    result = deploy_runner.run(DEPLOY_TEST_MAIN_SHA="0" * 40)

    assert result.returncode == 0
    assert "skipping" in result.stdout
    assert [call["tool"] for call in deploy_runner.calls()] == ["gh"]
    assert deploy_runner.remaining_work_directories() == []


def test_deploys_only_tracked_release_and_cleans_credentials(deploy_runner):
    result = deploy_runner.run()

    assert result.returncode == 0, result.stderr
    calls = deploy_runner.calls()
    upload = next(call for call in calls if call["tool"] == "scp")
    assert upload["uploaded_files"] == ["release.tar.gz", "deploy_preproduction.sh"]
    assert upload["archive_files"] == {
        "README.md": "A deployable test project.\n",
        "app.txt": "tracked application release\n",
        "scripts/deploy_preproduction.sh": "#!/bin/bash\nexit 0\n",
    }
    for call in calls:
        if call["tool"] in {"ssh", "scp"}:
            assert call["credential_modes"] == [0o600, 0o600]
            assert call["credential_env_present"] is False
    for name in ("DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS"):
        assert deploy_runner.env[name] not in result.stdout + result.stderr
    assert any(call["tool"] == "curl" for call in calls)
    assert calls[-1]["args"][-1] == f"rm -rf -- '{REMOTE_TEMP_DIR}'"
    assert deploy_runner.remaining_work_directories() == []
    assert (deploy_runner.repository / ".env").read_text() == "private fixture configuration\n"
    assert (deploy_runner.repository / "data" / "auth.sqlite3").read_text() == "private fixture user data\n"


@pytest.mark.parametrize("failure", ["upload", "remote"])
def test_remote_failure_is_not_reported_healthy_and_still_cleans_up(deploy_runner, failure):
    result = deploy_runner.run(DEPLOY_TEST_FAILURE=failure)

    assert result.returncode != 0
    assert "successfully" not in result.stdout
    calls = deploy_runner.calls()
    assert all(call["tool"] != "curl" for call in calls)
    assert calls[-1]["args"][-1] == f"rm -rf -- '{REMOTE_TEMP_DIR}'"
    assert deploy_runner.remaining_work_directories() == []
