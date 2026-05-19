from __future__ import annotations

import os
import subprocess

import docker
import docker.errors
import pytest


@pytest.fixture(scope="session")
def real_docker_client():
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:
        pytest.skip(f"Docker not available: {exc}")
    return client


@pytest.fixture(scope="session")
def zeroclaw_image(real_docker_client):
    image = os.environ.get("ZEROCLAW_TEST_IMAGE", "zeroclaw:latest")
    try:
        real_docker_client.images.get(image)
    except docker.errors.ImageNotFound:
        pytest.skip(f"image {image} not present locally")
    return image


@pytest.fixture
def real_data_dir(tmp_path_factory, real_docker_client):
    path = tmp_path_factory.mktemp("integration-data")
    yield path
    # Volume contents are chowned to UID 65534 (nobody) by the orchestrator's
    # busybox step. Pytest's teardown runs as the test user and cannot unlink
    # those files, which spams PermissionError warnings. Chown back via a
    # throwaway root busybox so tmp_path_factory can cleanly wipe the dir.
    try:
        real_docker_client.containers.run(
            "busybox",
            command=f"chown -R {os.getuid()}:{os.getgid()} /vol",
            user="root",
            volumes={str(path): {"bind": "/vol", "mode": "rw"}},
            remove=True,
        )
    except Exception:
        pass


@pytest.fixture
def cleanup_container(real_docker_client):
    def _cleanup(name_or_id: str) -> None:
        try:
            container = real_docker_client.containers.get(name_or_id)
        except Exception:
            return
        try:
            container.remove(force=True)
        except Exception:
            pass

    return _cleanup


@pytest.fixture(autouse=True)
def stub_get_profile(monkeypatch):
    """Integration tests don't exercise Supabase — mocking get_profile prevents
    the module-level httpx.AsyncClient in claw_proxy.config from leaking
    transports across per-test event loops."""
    async def _empty(_user_id: str) -> dict:
        return {}

    monkeypatch.setattr("claw_proxy.db.get_profile", _empty)


@pytest.fixture
def reclaim_volume(real_docker_client):
    """Chown a volume path back to the test user so host-side IO (write/read)
    works. The orchestrator chowns volumes to UID 65534 for the containerized
    ZeroClaw; admin operations on the host assume UID parity with the running
    proxy, which tests don't have."""
    def _reclaim(path: str) -> None:
        try:
            real_docker_client.containers.run(
                "busybox",
                command=f"chown -R {os.getuid()}:{os.getgid()} /vol",
                user="root",
                volumes={path: {"bind": "/vol", "mode": "rw"}},
                remove=True,
            )
        except Exception:
            pass

    return _reclaim
