"""Thin GraphQL client used by the integration tests.

We don't use stashapi here because we want tight control over the
lifecycle (scanning, waiting for jobs) without extra indirection.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import requests


class StashClient:
    def __init__(self, url: str):
        self.url = url
        self.graphql = f"{url}/graphql"

    def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = requests.post(
            self.graphql, json={"query": query, "variables": variables or {}}, timeout=30
        )
        resp.raise_for_status()
        body = resp.json()
        if "errors" in body:
            raise RuntimeError(f"GraphQL errors: {body['errors']}")
        return body["data"]

    # ---- configuration + scanning ----

    def setup(
        self,
        *,
        config_location: str = "/root/.stash/config.yml",
        stash_path: str = "/data",
        database_file: str = "/root/.stash/stash-go.sqlite",
        generated_location: str = "/root/.stash/generated",
        cache_location: str = "/root/.stash/cache",
        blobs_location: str = "/root/.stash/blobs",
    ) -> None:
        """Complete the one-time setup wizard programmatically.

        Required before any scene queries — without it, findScenes and
        friends panic because the database hasn't been initialised.
        """
        query = """
        mutation Setup($input: SetupInput!) { setup(input: $input) }
        """
        self.gql(
            query,
            {
                "input": {
                    "configLocation": config_location,
                    "stashes": [
                        {"path": stash_path, "excludeVideo": False, "excludeImage": True}
                    ],
                    "databaseFile": database_file,
                    "generatedLocation": generated_location,
                    "cacheLocation": cache_location,
                    "blobsLocation": blobs_location,
                    "storeBlobsInDatabase": False,
                }
            },
        )

    def set_library_path(
        self,
        path: str,
        ffmpeg_path: str = "/usr/bin/ffmpeg",
        ffprobe_path: str = "/usr/bin/ffprobe",
    ) -> None:
        query = """
        mutation ConfigureGeneral($input: ConfigGeneralInput!) {
          configureGeneral(input: $input) { stashes { path } ffmpegPath ffprobePath }
        }
        """
        self.gql(
            query,
            {
                "input": {
                    "stashes": [
                        {"path": path, "excludeVideo": False, "excludeImage": True}
                    ],
                    "ffmpegPath": ffmpeg_path,
                    "ffprobePath": ffprobe_path,
                }
            },
        )

    def metadata_scan(self) -> str:
        query = """
        mutation MetadataScan($input: ScanMetadataInput!) {
          metadataScan(input: $input)
        }
        """
        data = self.gql(query, {"input": {}})
        return data["metadataScan"]

    def wait_for_job(self, job_id: str, timeout: float = 60.0) -> None:
        query = """
        query Jobs { jobQueue { id status } }
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.gql(query)
            queue = data.get("jobQueue") or []
            job = next((j for j in queue if j["id"] == job_id), None)
            if job is None:
                return
            if job["status"] in {"FINISHED", "CANCELLED", "FAILED"}:
                if job["status"] == "FAILED":
                    raise RuntimeError(f"Job {job_id} failed")
                return
            time.sleep(0.5)
        raise TimeoutError(f"Job {job_id} did not finish within {timeout}s")

    # ---- scenes ----

    def find_scenes(self) -> list[dict[str, Any]]:
        query = """
        query FindScenes { findScenes(filter: {per_page: -1}) { scenes { id title } } }
        """
        return self.gql(query)["findScenes"]["scenes"]

    def set_cover_raw(self, scene_id: str, mime: str, raw_bytes: bytes) -> None:
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"
        query = """
        mutation SceneUpdate($input: SceneUpdateInput!) {
          sceneUpdate(input: $input) { id }
        }
        """
        self.gql(query, {"input": {"id": scene_id, "cover_image": data_url}})

    def fetch_cover(self, scene_id: str) -> bytes:
        resp = requests.get(f"{self.url}/scene/{scene_id}/screenshot", timeout=15)
        resp.raise_for_status()
        return resp.content
