"""Session-scoped fixtures for integration tests.

A single Stash container is reused across all integration test modules
to avoid paying the ~20s spin-up cost per file. Each test module is
responsible for its own seed data via module-scoped fixtures.
"""

from __future__ import annotations

import shutil

import pytest

from tests.integration.sandbox import StashSandbox, StashSandboxError
from tests.integration.stash_client import StashClient


@pytest.fixture(scope="session")
def sandbox():
    if shutil.which("docker") is None:
        pytest.skip("docker is required for integration tests")
    sb = StashSandbox()
    try:
        sb.start(timeout=180)
    except StashSandboxError as e:
        pytest.skip(f"sandbox unavailable: {e}")
    yield sb
    sb.stop()


@pytest.fixture(scope="session")
def client(sandbox):
    c = StashClient(sandbox.url)
    c.setup()
    c.set_library_path("/data")
    return c


@pytest.fixture(scope="session")
def seeded_scene(client, sandbox):
    scenes = client.find_scenes()
    if scenes:
        return scenes[0]
    job = client.metadata_scan()
    client.wait_for_job(job, timeout=120)
    scenes = client.find_scenes()
    if not scenes:
        pytest.fail("no scenes scanned; sandbox logs:\n" + sandbox.logs(tail=200))
    return scenes[0]
