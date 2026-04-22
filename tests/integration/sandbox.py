"""Ephemeral Stash sandbox lifecycle helpers.

Starts the docker-compose stack in `tests/sandbox`, waits for Stash to be
healthy, and exposes the host URL. `stop()` tears everything down including
volumes, so each test run is hermetic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
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

    def _wait_for_graphql(self, timeout: float) -> None:
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
