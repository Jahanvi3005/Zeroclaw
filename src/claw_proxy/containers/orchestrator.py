"""Container orchestrator — manages ZeroClaw container lifecycle via Docker."""

import asyncio
import dataclasses
import logging
import os
import secrets
from typing import cast

import docker
import docker.errors
import httpx
from docker.models.containers import Container

from claw_proxy.containers.workspace import (
    WORKSPACE_INIT_SENTINEL,
    read_installed_skill_version,
    read_template_skill_version,
    render_lifeatlas_skill,
    render_lifeatlas_skill_token,
)

log = logging.getLogger(__name__)

WORKSPACE_DIRS = (
    "workspace/temp",
    "workspace/lifeatlas/files",
    "workspace/lifeatlas/photos",
)
VALID_NETWORK_MODES = {"host", "shared"}
PROVIDER_API_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google": "GEMINI_API_KEY",
    "google-gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY",
    "grok": "XAI_API_KEY",
    "together": "TOGETHER_API_KEY",
    "together-ai": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "fireworks-ai": "FIREWORKS_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "cohere": "COHERE_API_KEY",
    "venice": "VENICE_API_KEY",
    "ollama": "OLLAMA_API_KEY",
    "bedrock": "BEDROCK_API_KEY",
    "aws-bedrock": "BEDROCK_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY",
    "azure-openai": "AZURE_OPENAI_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
}


def _parse_skill_version(version: str | None) -> tuple[int, ...] | None:
    if version is None:
        return None
    parts = version.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _template_version_is_newer(
    *,
    installed: str | None,
    template: str | None,
) -> bool:
    parsed_template = _parse_skill_version(template)
    if parsed_template is None:
        return False
    parsed_installed = _parse_skill_version(installed)
    if parsed_installed is None:
        return installed is None

    width = max(len(parsed_installed), len(parsed_template))
    padded_installed = parsed_installed + (0,) * (width - len(parsed_installed))
    padded_template = parsed_template + (0,) * (width - len(parsed_template))
    return padded_template > padded_installed


@dataclasses.dataclass
class ContainerInfo:
    container_id: str
    ws_url: str
    http_url: str
    bearer_token: str
    status: str
    current_session_id: str | None = None

    def __getitem__(self, key: str):
        return getattr(self, key)


def build_container_urls(
    user_id: str,
    network_mode: str,
    host_port: str | None,
) -> tuple[str, str]:
    if network_mode not in VALID_NETWORK_MODES:
        raise ValueError(
            "Invalid ZEROCLAW_NETWORK_MODE: expected 'host' or 'shared', "
            f"got {network_mode!r}"
        )
    if network_mode == "shared":
        host = f"zeroclaw-{user_id[:8]}"
        http_url = f"http://{host}:42617"
        ws_url = f"ws://{host}:42617/ws/chat"
        return http_url, ws_url
    if host_port is None:
        raise ValueError("host_port is required when network_mode='host'")
    http_url = f"http://localhost:{host_port}"
    ws_url = f"ws://localhost:{host_port}/ws/chat"
    return http_url, ws_url


def build_llm_container_env(llm_env: dict[str, str]) -> dict[str, str]:
    """Translate proxy LLM env into ZeroClaw's runtime env contract."""
    env: dict[str, str] = {}
    provider = llm_env.get("ZEROCLAW_LLM_PROVIDER")
    model = llm_env.get("ZEROCLAW_LLM_MODEL")
    api_key = llm_env.get("ZEROCLAW_LLM_API_KEY")

    if provider:
        env["ZEROCLAW_PROVIDER"] = provider
    if model:
        env["ZEROCLAW_MODEL"] = model
    if api_key:
        provider_key = (provider or "").strip().lower()
        api_key_env = PROVIDER_API_KEY_ENV.get(provider_key)
        env[api_key_env or "ZEROCLAW_API_KEY"] = api_key

    return env


class ContainerOrchestrator:
    def __init__(
        self,
        registry,  # db.ContainerRegistry
        docker_client=None,
        image: str = "zeroclaw:latest",
        data_dir: str = "/data/zeroclaw",
        host_data_dir: str | None = None,
        llm_env: dict[str, str] | None = None,
        push_webhook_base_url: str = "http://172.17.0.1:8000",
        templates_dir: str = "",
        network_mode: str = "host",
        network_name: str = "lifeatlas-net",
    ) -> None:
        self.registry = registry
        self.docker = docker_client or docker.from_env()
        self.image = image
        self.data_dir = data_dir
        self.host_data_dir = host_data_dir or data_dir
        self.llm_env = llm_env or {}
        self.token_map: dict[str, str] = {}
        self.push_webhook_base_url = push_webhook_base_url.rstrip("/")
        # __file__ lives at src/claw_proxy/containers/orchestrator.py;
        # templates/ ships at the repo root.
        self.templates_dir = templates_dir or os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "templates")
        )
        if network_mode not in VALID_NETWORK_MODES:
            raise ValueError(
                "Invalid ZEROCLAW_NETWORK_MODE: expected 'host' or 'shared', "
                f"got {network_mode!r}"
            )
        self.network_mode = network_mode
        self.network_name = network_name
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, user_id: str) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    async def _ensure_skills_current(self, user_id: str, token: str) -> bool:
        volume_path = f"{self.data_dir}/{user_id}"
        host_volume_path = f"{self.host_data_dir}/{user_id}"
        installed = await asyncio.to_thread(read_installed_skill_version, volume_path)
        template = await asyncio.to_thread(
            read_template_skill_version,
            self.templates_dir,
        )
        if not _template_version_is_newer(installed=installed, template=template):
            return False

        log.info(
            "Retrofit LifeAtlas skill for user %s: installed=%s template=%s",
            user_id[:8],
            installed,
            template,
        )
        await asyncio.to_thread(
            render_lifeatlas_skill,
            docker_client=self.docker,
            templates_dir=self.templates_dir,
            volume_path=volume_path,
            host_volume_path=host_volume_path,
            token=token,
        )
        return True

    async def get(self, user_id: str) -> ContainerInfo | None:
        row = await self.registry.get(user_id)
        if not row:
            return None
        token = row.get("bearer_token", "")
        if token and token not in self.token_map:
            self.token_map[token] = user_id
        http_url = row.get("http_url", "")
        ws_url = row.get("ws_url", "")
        if self.network_mode == "shared":
            http_url, ws_url = build_container_urls(
                user_id=user_id,
                network_mode=self.network_mode,
                host_port=None,
            )
        return ContainerInfo(
            container_id=row["container_id"],
            ws_url=ws_url,
            http_url=http_url,
            bearer_token=token,
            status=row.get("status", ""),
            current_session_id=row.get("current_session_id"),
        )

    async def provision(self, user_id: str) -> ContainerInfo:
        async with self._get_lock(user_id):
            # Re-check after acquiring lock
            existing = await self.get(user_id)
            if existing:
                return existing

            token = secrets.token_urlsafe(32)
            volume_path = f"{self.data_dir}/{user_id}"
            host_volume_path = f"{self.host_data_dir}/{user_id}"
            created_workspace_sentinel = False
            os.makedirs(volume_path, exist_ok=True)
            for relative_dir in WORKSPACE_DIRS:
                os.makedirs(os.path.join(volume_path, relative_dir), exist_ok=True)

            if self.templates_dir:
                from claw_proxy.db import get_profile
                from claw_proxy.containers.workspace import (
                    copy_template_files,
                    is_workspace_initialized,
                    mark_workspace_initialized,
                    patch_config_toml,
                    patch_user_md,
                    resolve_template_dir,
                )

                if not is_workspace_initialized(volume_path):
                    profile = await get_profile(user_id)
                    first_name = profile.get("first_name")
                    last_name = profile.get("last_name")
                    dob = profile.get("date_of_birth")
                    default_template_dir = os.path.join(self.templates_dir, "default")
                    template_dir = resolve_template_dir(first_name, self.templates_dir)

                    if not os.path.isdir(default_template_dir):
                        raise RuntimeError(
                            f"Default template directory not found: "
                            f"{default_template_dir!r}. Check templates_dir "
                            f"(currently {self.templates_dir!r})."
                        )
                    copy_template_files(default_template_dir, volume_path)
                    if template_dir != default_template_dir and os.path.isdir(
                        template_dir
                    ):
                        copy_template_files(template_dir, volume_path)
                    patch_config_toml(
                        os.path.join(volume_path, ".zeroclaw", "config.toml"),
                        provider=self.llm_env.get("ZEROCLAW_LLM_PROVIDER"),
                        model=self.llm_env.get("ZEROCLAW_LLM_MODEL"),
                    )
                    patch_user_md(
                        os.path.join(volume_path, "workspace", "USER.md"),
                        first_name,
                        last_name,
                        str(dob) if dob else None,
                    )
                    await self._ensure_skills_current(user_id, token)
                    render_lifeatlas_skill_token(volume_path, token)
                    mark_workspace_initialized(volume_path)
                    created_workspace_sentinel = True
                else:
                    log.info("Workspace already initialized for user %s", user_id[:8])
                    await self._ensure_skills_current(user_id, token)

            try:
                # chown volume to container user (nobody/65534) via throwaway root container
                await asyncio.to_thread(
                    self.docker.containers.run,
                    "busybox",
                    command=f"chown -R 65534:65534 /vol",
                    user="root",
                    volumes={host_volume_path: {"bind": "/vol", "mode": "rw"}},
                    remove=True,
                )

                env = {
                    "ZEROCLAW_TOKEN": token,
                    "ZEROCLAW_CHANNELS_LIFEATLAS_ENABLED": "true",
                    "ZEROCLAW_CHANNELS_LIFEATLAS_WEBHOOK_URL": (
                        f"{self.push_webhook_base_url}/zeroclaw/push"
                    ),
                    "ZEROCLAW_CHANNELS_LIFEATLAS_AUTH_TOKEN": token,
                    "ZEROCLAW_GATEWAY_HOST": "0.0.0.0",
                    "ZEROCLAW_GATEWAY_ALLOW_PUBLIC_BIND": "true",
                    "ZEROCLAW_REQUIRE_PAIRING": "false",
                }
                env.update(build_llm_container_env(self.llm_env))

                run_kwargs = {
                    "detach": True,
                    "environment": env,
                    "volumes": {host_volume_path: {"bind": "/zeroclaw-data", "mode": "rw"}},
                    "name": f"zeroclaw-{user_id[:8]}",
                }
                if self.network_mode == "shared":
                    run_kwargs["network"] = self.network_name
                else:
                    run_kwargs["publish_all_ports"] = True

                container = cast(
                    Container,
                    await asyncio.to_thread(
                        self.docker.containers.run,
                        self.image,
                        **run_kwargs,
                    ),
                )
                container_id: str = container.id or ""

                host_port: str | None = None
                if self.network_mode == "host":
                    await asyncio.to_thread(container.reload)
                    ports = container.attrs["NetworkSettings"]["Ports"]
                    host_port = ports["42617/tcp"][0]["HostPort"]
                http_url, ws_url = build_container_urls(
                    user_id=user_id,
                    network_mode=self.network_mode,
                    host_port=host_port,
                )

                # Wait for health (config.toml now exists)
                await self._wait_healthy(http_url, timeout=30)

                try:
                    await self.registry.insert(
                        user_id=user_id,
                        container_id=container_id,
                        ws_url=ws_url,
                        http_url=http_url,
                        bearer_token=token,
                        status="ready",
                    )
                except Exception:
                    log.error(
                        "Registry insert failed, cleaning up container %s",
                        container_id[:12],
                    )
                    await asyncio.to_thread(container.stop, timeout=5)
                    await asyncio.to_thread(container.remove)
                    raise
            except Exception:
                if created_workspace_sentinel:
                    await self._remove_provisional_workspace_sentinel(
                        volume_path,
                        host_volume_path,
                    )
                raise

            self.token_map[token] = user_id
            log.info(
                "Provisioned container %s for user %s", container_id[:12], user_id[:8]
            )

            return ContainerInfo(
                container_id=container_id,
                ws_url=ws_url,
                http_url=http_url,
                bearer_token=token,
                status="ready",
                current_session_id=None,
            )

    async def _remove_provisional_workspace_sentinel(
        self,
        volume_path: str,
        host_volume_path: str,
    ) -> None:
        marker_path = os.path.join(volume_path, WORKSPACE_INIT_SENTINEL)
        try:
            os.remove(marker_path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning(
                "Failed to remove provisional workspace sentinel %s via host: %s",
                marker_path,
                exc,
            )

        try:
            await asyncio.to_thread(
                self.docker.containers.run,
                "busybox",
                command=[
                    "sh",
                    "-ceu",
                    'rm -f -- "$1"',
                    "sh",
                    f"/vol/{WORKSPACE_INIT_SENTINEL}",
                ],
                user="root",
                volumes={host_volume_path: {"bind": "/vol", "mode": "rw"}},
                remove=True,
            )
        except Exception as exc:
            log.warning(
                "Failed to remove provisional workspace sentinel %s via busybox: %s",
                marker_path,
                exc,
            )

    async def ensure_workspace_directories(self) -> None:
        from claw_proxy.containers.workspace import WORKSPACE_INIT_SENTINEL, ensure_directory_via_busybox

        if not os.path.isdir(self.data_dir):
            return

        for entry in os.listdir(self.data_dir):
            user_dir = os.path.join(self.data_dir, entry)
            if not os.path.isdir(user_dir):
                continue
            if not os.path.exists(os.path.join(user_dir, WORKSPACE_INIT_SENTINEL)):
                continue
            for relative_dir in WORKSPACE_DIRS:
                target = os.path.join(user_dir, relative_dir)
                if os.path.isdir(target):
                    continue
                host_user_dir = os.path.join(self.host_data_dir, entry)
                await asyncio.to_thread(
                    ensure_directory_via_busybox,
                    self.docker,
                    host_user_dir,
                    relative_dir,
                )

    async def stop(self, user_id: str) -> None:
        info = await self.get(user_id)
        if not info:
            return
        try:
            container = await asyncio.to_thread(
                self.docker.containers.get, info.container_id
            )
            await asyncio.to_thread(container.stop, timeout=10)
            await self.registry.update_status(user_id, "stopped")
            log.info("Stopped container %s", info.container_id[:12])
        except docker.errors.NotFound:
            await self.registry.update_status(user_id, "stopped")
        except Exception as e:
            log.error("Failed to stop container %s: %s", info.container_id[:12], e)
            raise

    async def start(self, user_id: str) -> ContainerInfo:
        info = await self.get(user_id)
        if not info:
            raise RuntimeError(f"No container for user {user_id}")
        try:
            container = await asyncio.to_thread(
                self.docker.containers.get, info.container_id
            )
            await self._ensure_skills_current(user_id, info.bearer_token)
            await asyncio.to_thread(container.start)

            host_port: str | None = None
            if self.network_mode == "host":
                await asyncio.to_thread(container.reload)
                ports = container.attrs["NetworkSettings"]["Ports"]
                host_port = ports["42617/tcp"][0]["HostPort"]
            http_url, ws_url = build_container_urls(
                user_id=user_id,
                network_mode=self.network_mode,
                host_port=host_port,
            )

            await self._wait_healthy(http_url, timeout=30)
            await self.registry.update_urls(user_id, ws_url, http_url)
            await self.registry.update_status(user_id, "ready")
            return ContainerInfo(
                container_id=info.container_id,
                ws_url=ws_url,
                http_url=http_url,
                bearer_token=info.bearer_token,
                status="ready",
                current_session_id=info.current_session_id,
            )
        except docker.errors.NotFound:
            await self.registry.delete(user_id)
            return await self.provision(user_id)

    async def restart(self, user_id: str) -> ContainerInfo:
        info = await self.get(user_id)
        if not info:
            raise RuntimeError(f"No container for user {user_id}")
        try:
            container = await asyncio.to_thread(
                self.docker.containers.get, info.container_id
            )
            await self._ensure_skills_current(user_id, info.bearer_token)
            await asyncio.to_thread(container.restart, timeout=10)

            host_port: str | None = None
            if self.network_mode == "host":
                await asyncio.to_thread(container.reload)
                ports = container.attrs["NetworkSettings"]["Ports"]
                host_port = ports["42617/tcp"][0]["HostPort"]
            http_url, ws_url = build_container_urls(
                user_id=user_id,
                network_mode=self.network_mode,
                host_port=host_port,
            )

            await self._wait_healthy(http_url, timeout=30)
            await self.registry.update_urls(user_id, ws_url, http_url)
            await self.registry.update_status(user_id, "ready")
            return ContainerInfo(
                container_id=info.container_id,
                ws_url=ws_url,
                http_url=http_url,
                bearer_token=info.bearer_token,
                status="ready",
                current_session_id=info.current_session_id,
            )
        except docker.errors.NotFound:
            # Container was removed, reprovision
            await self.registry.delete(user_id)
            return await self.provision(user_id)

    async def update_activity(self, user_id: str) -> None:
        await self.registry.update_activity(user_id)

    async def _wait_healthy(self, http_url: str, timeout: float = 30) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with httpx.AsyncClient() as client:
            while loop.time() < deadline:
                try:
                    resp = await client.get(f"{http_url}/health", timeout=2)
                    if resp.status_code == 200:
                        return
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.5)
        raise TimeoutError(f"Container at {http_url} not healthy after {timeout}s")
