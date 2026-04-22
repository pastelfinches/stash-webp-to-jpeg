"""Ephemeral Stash sandbox lifecycle helpers.

Starts the docker-compose stack in `tests/sandbox`, waits for Stash to be
healthy, and exposes the host URL. `stop()` tears everything down including
volumes, so each test run is hermetic.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import requests

SANDBOX_DIR = Path(__file__).resolve().parents[1] / "sandbox"
DEFAULT_HOST_PORT = int(os.environ.get("STASH_HOST_PORT", "19999"))


class StashSandboxError(RuntimeError):
    pass


class StashSandbox:
    def __init__(self, host_port: int = DEFAULT_HOST_PORT):
        self.host_port = host_port
        self.url = f"http://127.0.0.1:{host_port}"
        self._compose_env = {**os.environ, "STASH_HOST_PORT": str(host_port)}

    def _compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["docker", "compose", "-f", str(SANDBOX_DIR / "docker-compose.yml"), *args]
        return subprocess.run(
            cmd,
            cwd=SANDBOX_DIR,
            env=self._compose_env,
            check=check,
            capture_output=True,
            text=True,
        )

    def start(self, timeout: float = 120.0) -> None:
        if shutil.which("docker") is None:
            raise StashSandboxError("docker not available")
        self._compose("up", "-d", "--wait")
        self._wait_for_graphql(timeout=timeout)

    def stop(self) -> None:
        try:
            self._compose("down", "-v", "--remove-orphans", check=False)
        except Exception:
            pass

    def logs(self, tail: int = 200) -> str:
        result = self._compose("logs", "--tail", str(tail), check=False)
        return result.stdout + result.stderr

    def exec(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        container = self._compose("ps", "-q", "stash").stdout.strip()
        if not container:
            raise StashSandboxError("sandbox is not running")
        return subprocess.run(
            ["docker", "exec", container, *args],
            check=check,
            capture_output=True,
            text=True,
        )

    def copy_into(self, host_path: Path, container_path: str) -> None:
        container = self._compose("ps", "-q", "stash").stdout.strip()
        if not container:
            raise StashSandboxError("sandbox is not running")
        subprocess.run(
            ["docker", "cp", str(host_path), f"{container}:{container_path}"],
            check=True,
        )

    def download_pythondepmanager(self, dest: Path) -> Path:
        """Fetch the PythonDepManager plugin directory from CommunityScripts.

        Returns the local path to the extracted `PythonDepManager/` dir,
        ready to be copy_into()'d at /root/.stash/plugins/PythonDepManager.
        """
        resp = requests.get(
            "https://github.com/stashapp/CommunityScripts/archive/refs/heads/main.tar.gz",
            stream=True,
            timeout=60,
        )
        resp.raise_for_status()
        buf = io.BytesIO(resp.content)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            prefix_needle = "/plugins/PythonDepManager/"
            for member in tar.getmembers():
                if prefix_needle not in member.name:
                    continue
                rel = member.name.split(prefix_needle, 1)[1]
                target = dest / rel
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(member)
                if src is None:
                    continue
                target.write_bytes(src.read())
        if not any(dest.iterdir()):
            raise StashSandboxError(
                "PythonDepManager not found in CommunityScripts tarball"
            )
        return dest

    def _wait_for_graphql(self, timeout: float) -> None:  # noqa: D401
        """Poll GraphQL until it returns version info or we time out."""
        deadline = time.monotonic() + timeout
        query = {"query": "{ version { version } }"}
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                resp = requests.post(f"{self.url}/graphql", json=query, timeout=3)
                if resp.status_code == 200 and "data" in resp.json():
                    return
            except Exception as e:
                last_err = e
            time.sleep(1.0)
        raise StashSandboxError(
            f"Stash did not become ready at {self.url} within {timeout}s: {last_err}"
        )
