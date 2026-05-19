import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import FAKE_USER, FAKE_ENCRYPTION_KEY


@pytest.fixture
def mock_docker():
    """Mock Docker SDK client."""
    with patch("claw_proxy.containers.orchestrator.docker") as mock_mod:
        client = MagicMock()
        mock_mod.from_env.return_value = client
        yield client


@pytest.fixture
def mock_registry():
    return AsyncMock()


@pytest.fixture
def orch(mock_docker, mock_registry, tmp_path):
    from claw_proxy.containers.orchestrator import ContainerOrchestrator

    templates_dir = tmp_path / "templates"
    default_dir = templates_dir / "default"
    (default_dir / ".zeroclaw").mkdir(parents=True)
    (default_dir / ".zeroclaw" / "config.toml").write_text(
        'workspace_dir = "/zeroclaw-data/workspace"\n'
        '\n'
        '[agents.research]\n'
        'provider = "openrouter"\n'
        'model = "stale-model"\n'
    )
    (default_dir / "workspace").mkdir()

    return ContainerOrchestrator(
        registry=mock_registry,
        docker_client=mock_docker,
        image="zeroclaw:latest",
        data_dir=str(tmp_path / "data"),
        llm_env={
            "ZEROCLAW_LLM_API_KEY": "llm-key",
            "ZEROCLAW_LLM_PROVIDER": "anthropic",
            "ZEROCLAW_LLM_MODEL": "claude-sonnet-4-6",
        },
        push_webhook_base_url="https://proxy.example",
        templates_dir=str(templates_dir),
    )


class TestDefaultTemplatesDir:
    """Regression coverage for the default templates_dir resolution.

    Why this test exists: the src/ layout restructure silently broke the
    default path computation in orchestrator.py — every existing test passed
    templates_dir explicitly, so the fallback branch had zero coverage and
    production deploys without ZEROCLAW_TEMPLATES_DIR shipped templates from
    a directory that didn't exist.
    """

    def test_default_templates_dir_resolves_to_real_directory(
        self, mock_docker, mock_registry
    ):
        from claw_proxy.containers.orchestrator import ContainerOrchestrator

        orch = ContainerOrchestrator(registry=mock_registry, docker_client=mock_docker)
        default_dir = pathlib.Path(orch.templates_dir) / "default"
        assert default_dir.is_dir(), (
            f"Default templates dir {orch.templates_dir!r} missing — "
            "package layout likely changed without updating the path computation."
        )

    @pytest.mark.asyncio
    async def test_provision_raises_when_templates_dir_missing(
        self, mock_docker, mock_registry, tmp_path
    ):
        """Silent no-op on missing templates is the bug shape — must raise."""
        from claw_proxy.containers.orchestrator import ContainerOrchestrator

        orch = ContainerOrchestrator(
            registry=mock_registry,
            docker_client=mock_docker,
            data_dir=str(tmp_path / "data"),
            templates_dir=str(tmp_path / "does_not_exist"),
        )
        mock_registry.get = AsyncMock(return_value=None)

        with patch("claw_proxy.db.get_profile", new=AsyncMock(return_value={})):
            with pytest.raises(RuntimeError, match="Default template directory"):
                await orch.provision(FAKE_USER["id"])


class TestProvision:
    @pytest.mark.asyncio
    async def test_provision_new(self, orch, mock_docker, mock_registry):
        mock_registry.get = AsyncMock(return_value=None)
        mock_registry.insert = AsyncMock()

        container = MagicMock()
        container.id = "container_abc"
        container.attrs = {
            "NetworkSettings": {
                "Ports": {"42617/tcp": [{"HostPort": "32768"}]}
            }
        }
        mock_docker.containers.run.return_value = container
        container.reload = MagicMock()
        container.exec_run = MagicMock()

        # First containers.run call is the busybox chown, second is the real container
        mock_docker.containers.run.side_effect = [None, container]

        with patch("claw_proxy.containers.orchestrator.httpx") as mock_httpx, \
             patch("claw_proxy.db.get_profile", new=AsyncMock(return_value={})):
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_httpx.AsyncClient.return_value = mock_client

            info = await orch.provision(FAKE_USER["id"])

        assert info["container_id"] == "container_abc"
        assert "32768" in info["ws_url"]
        mock_registry.insert.assert_called_once()
        assert orch.token_map[info["bearer_token"]] == FAKE_USER["id"]
        # Second call is the zeroclaw container
        env = mock_docker.containers.run.call_args_list[1].kwargs["environment"]
        assert env["ZEROCLAW_TOKEN"] == info["bearer_token"]
        assert env["ZEROCLAW_CHANNELS_LIFEATLAS_ENABLED"] == "true"
        assert env["ZEROCLAW_CHANNELS_LIFEATLAS_WEBHOOK_URL"] == "https://proxy.example/zeroclaw/push"
        assert env["ZEROCLAW_CHANNELS_LIFEATLAS_AUTH_TOKEN"] == info["bearer_token"]
        assert env["ZEROCLAW_GATEWAY_HOST"] == "0.0.0.0"
        assert env["ZEROCLAW_GATEWAY_ALLOW_PUBLIC_BIND"] == "true"
        assert env["ZEROCLAW_REQUIRE_PAIRING"] == "false"
        assert env["ZEROCLAW_PROVIDER"] == "anthropic"
        assert env["ZEROCLAW_MODEL"] == "claude-sonnet-4-6"
        assert env["ANTHROPIC_API_KEY"] == "llm-key"
        assert "API_KEY" not in env
        assert "ZEROCLAW_API_KEY" not in env
        container.exec_run.assert_not_called()

        # Workspace was scaffolded from templates and provider/model were patched in.
        # Without this assertion the bug shape "templates_dir points at nothing,
        # provision succeeds with an empty volume" passes silently.
        import tomllib
        config_path = (
            pathlib.Path(orch.data_dir) / FAKE_USER["id"] / ".zeroclaw" / "config.toml"
        )
        assert config_path.is_file()
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        assert config["providers"]["fallback"] == "anthropic"
        provider_entry = config["providers"]["models"]["anthropic"]
        assert provider_entry["default_provider"] == "anthropic"
        assert provider_entry["model"] == "claude-sonnet-4-6"
        assert config["agents"]["research"]["provider"] == "anthropic"
        assert config["agents"]["research"]["model"] == "claude-sonnet-4-6"
        assert "default_provider" not in config
        assert "default_model" not in config

    @pytest.mark.asyncio
    async def test_provision_creates_workspace_dirs(self, orch, mock_docker, mock_registry):
        mock_registry.get = AsyncMock(return_value=None)
        mock_registry.insert = AsyncMock()

        container = MagicMock()
        container.id = "container_abc"
        container.attrs = {
            "NetworkSettings": {"Ports": {"42617/tcp": [{"HostPort": "32768"}]}}
        }
        container.reload = MagicMock()
        mock_docker.containers.run.side_effect = [None, container]

        with patch("claw_proxy.containers.orchestrator.httpx") as mock_httpx, patch(
            "claw_proxy.db.get_profile",
            new=AsyncMock(return_value={}),
        ):
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_httpx.AsyncClient.return_value = mock_client

            await orch.provision(FAKE_USER["id"])

        user_dir = pathlib.Path(orch.data_dir) / FAKE_USER["id"]
        assert (user_dir / "workspace" / "temp").is_dir()
        assert (user_dir / "workspace" / "lifeatlas" / "files").is_dir()
        assert (user_dir / "workspace" / "lifeatlas" / "photos").is_dir()

    @pytest.mark.asyncio
    async def test_provision_shared_network_mode_uses_deterministic_container_urls(
        self, mock_docker, mock_registry, tmp_path
    ):
        from claw_proxy.containers.orchestrator import ContainerOrchestrator

        mock_registry.get = AsyncMock(return_value=None)
        mock_registry.insert = AsyncMock()

        container = MagicMock()
        container.id = "container_shared"
        container.attrs = {"NetworkSettings": {"Ports": {"42617/tcp": None}}}
        container.reload = MagicMock()
        mock_docker.containers.run.side_effect = [None, container]
        templates_dir = tmp_path / "templates"
        (templates_dir / "default").mkdir(parents=True)

        orch = ContainerOrchestrator(
            registry=mock_registry,
            docker_client=mock_docker,
            image="zeroclaw:latest",
            data_dir=str(tmp_path / "data"),
            host_data_dir="/host/zeroclaw",
            llm_env={"ZEROCLAW_LLM_API_KEY": "llm-key"},
            push_webhook_base_url="https://proxy.example",
            templates_dir=str(templates_dir),
            network_mode="shared",
            network_name="lifeatlas-net",
        )

        with patch("claw_proxy.containers.orchestrator.httpx") as mock_httpx, patch(
            "claw_proxy.db.get_profile",
            new=AsyncMock(return_value={}),
        ):
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_httpx.AsyncClient.return_value = mock_client

            info = await orch.provision(FAKE_USER["id"])

        expected_host = f"zeroclaw-{FAKE_USER['id'][:8]}"
        assert info.ws_url == f"ws://{expected_host}:42617/ws/chat"
        assert info.http_url == f"http://{expected_host}:42617"
        run_kwargs = mock_docker.containers.run.call_args_list[1].kwargs
        assert "publish_all_ports" not in run_kwargs
        assert run_kwargs["network"] == "lifeatlas-net"
        assert run_kwargs["volumes"] == {
            f"/host/zeroclaw/{FAKE_USER['id']}": {
                "bind": "/zeroclaw-data",
                "mode": "rw",
            }
        }
        chown_kwargs = mock_docker.containers.run.call_args_list[0].kwargs
        assert chown_kwargs["volumes"] == {
            f"/host/zeroclaw/{FAKE_USER['id']}": {"bind": "/vol", "mode": "rw"}
        }

    def test_build_llm_container_env_uses_generic_key_for_unknown_provider(self):
        from claw_proxy.containers.orchestrator import build_llm_container_env

        env = build_llm_container_env(
            {
                "ZEROCLAW_LLM_PROVIDER": "custom:https://llm.example/v1",
                "ZEROCLAW_LLM_MODEL": "model-a",
                "ZEROCLAW_LLM_API_KEY": "llm-key",
            }
        )

        assert env == {
            "ZEROCLAW_PROVIDER": "custom:https://llm.example/v1",
            "ZEROCLAW_MODEL": "model-a",
            "ZEROCLAW_API_KEY": "llm-key",
        }

    @pytest.mark.asyncio
    async def test_provision_already_exists(self, orch, mock_registry):
        mock_registry.get = AsyncMock(return_value={
            "container_id": "existing",
            "ws_url": "ws://localhost:32000/ws/chat",
            "http_url": "http://localhost:32000",
            "bearer_token": "tok",
            "status": "ready",
            "current_session_id": None,
        })
        info = await orch.provision(FAKE_USER["id"])
        assert info["container_id"] == "existing"

    @pytest.mark.asyncio
    async def test_get_populates_token_map_from_registry(self, orch, mock_registry):
        mock_registry.get = AsyncMock(return_value={
            "container_id": "existing",
            "ws_url": "ws://localhost:32000/ws/chat",
            "http_url": "http://localhost:32000",
            "bearer_token": "tok-existing",
            "status": "ready",
            "current_session_id": "sess-existing",
        })

        info = await orch.get(FAKE_USER["id"])

        assert info["container_id"] == "existing"
        assert orch.token_map["tok-existing"] == FAKE_USER["id"]
        assert info.current_session_id == "sess-existing"

    @pytest.mark.asyncio
    async def test_get_shared_network_mode_rebuilds_urls_from_user_id(
        self, mock_docker, mock_registry, tmp_path
    ):
        from claw_proxy.containers.orchestrator import ContainerOrchestrator

        mock_registry.get = AsyncMock(return_value={
            "container_id": "existing",
            "ws_url": "ws://stale-host:9999/ws/chat",
            "http_url": "http://stale-host:9999",
            "bearer_token": "tok-shared",
            "status": "ready",
            "current_session_id": "sess-shared",
        })

        orch = ContainerOrchestrator(
            registry=mock_registry,
            docker_client=mock_docker,
            data_dir=str(tmp_path / "data"),
            network_mode="shared",
            network_name="lifeatlas-net",
        )

        info = await orch.get(FAKE_USER["id"])

        expected_host = f"zeroclaw-{FAKE_USER['id'][:8]}"
        assert info.http_url == f"http://{expected_host}:42617"
        assert info.ws_url == f"ws://{expected_host}:42617/ws/chat"
        assert orch.token_map["tok-shared"] == FAKE_USER["id"]

    @pytest.mark.asyncio
    async def test_start_ensures_skills_current_before_container_start(
        self, orch, mock_docker, mock_registry
    ):
        mock_registry.get = AsyncMock(return_value={
            "container_id": "existing",
            "ws_url": "ws://localhost:32000/ws/chat",
            "http_url": "http://localhost:32000",
            "bearer_token": "tok-start",
            "status": "stopped",
            "current_session_id": "sess-start",
        })
        mock_registry.update_urls = AsyncMock()
        mock_registry.update_status = AsyncMock()

        calls = []
        container = MagicMock()
        container.attrs = {
            "NetworkSettings": {"Ports": {"42617/tcp": [{"HostPort": "32768"}]}}
        }
        container.start.side_effect = lambda: calls.append("start")
        mock_docker.containers.get.return_value = container

        async def ensure(user_id, token):
            calls.append("ensure")

        with patch.object(
            orch,
            "_ensure_skills_current",
            new=AsyncMock(side_effect=ensure),
        ) as mock_ensure, patch.object(
            orch,
            "_wait_healthy",
            new=AsyncMock(),
        ):
            await orch.start(FAKE_USER["id"])

        mock_ensure.assert_awaited_once_with(FAKE_USER["id"], "tok-start")
        assert calls == ["ensure", "start"]

    @pytest.mark.asyncio
    async def test_restart_shared_network_mode_rebuilds_urls_without_host_port(
        self, mock_docker, mock_registry, tmp_path
    ):
        from claw_proxy.containers.orchestrator import ContainerOrchestrator

        mock_registry.get = AsyncMock(return_value={
            "container_id": "existing",
            "ws_url": "ws://stale-host:9999/ws/chat",
            "http_url": "http://stale-host:9999",
            "bearer_token": "tok-shared",
            "status": "stopped",
            "current_session_id": "sess-shared",
        })
        mock_registry.update_urls = AsyncMock()
        mock_registry.update_status = AsyncMock()

        container = MagicMock()
        container.restart = MagicMock()
        mock_docker.containers.get.return_value = container

        orch = ContainerOrchestrator(
            registry=mock_registry,
            docker_client=mock_docker,
            data_dir=str(tmp_path / "data"),
            network_mode="shared",
            network_name="lifeatlas-net",
        )

        with patch.object(orch, "_wait_healthy", new=AsyncMock()) as mock_wait_healthy:
            info = await orch.restart(FAKE_USER["id"])

        expected_host = f"zeroclaw-{FAKE_USER['id'][:8]}"
        expected_http_url = f"http://{expected_host}:42617"
        expected_ws_url = f"ws://{expected_host}:42617/ws/chat"
        container.restart.assert_called_once_with(timeout=10)
        mock_registry.update_urls.assert_awaited_once_with(
            FAKE_USER["id"], expected_ws_url, expected_http_url
        )
        mock_registry.update_status.assert_awaited_once_with(FAKE_USER["id"], "ready")
        mock_wait_healthy.assert_awaited_once_with(expected_http_url, timeout=30)
        assert info.http_url == expected_http_url
        assert info.ws_url == expected_ws_url

    @pytest.mark.asyncio
    async def test_restart_ensures_skills_current_before_container_restart(
        self, orch, mock_docker, mock_registry
    ):
        mock_registry.get = AsyncMock(return_value={
            "container_id": "existing",
            "ws_url": "ws://localhost:32000/ws/chat",
            "http_url": "http://localhost:32000",
            "bearer_token": "tok-restart",
            "status": "ready",
            "current_session_id": "sess-restart",
        })
        mock_registry.update_urls = AsyncMock()
        mock_registry.update_status = AsyncMock()

        calls = []
        container = MagicMock()
        container.attrs = {
            "NetworkSettings": {"Ports": {"42617/tcp": [{"HostPort": "32768"}]}}
        }
        container.restart.side_effect = lambda timeout: calls.append(
            ("restart", timeout)
        )
        mock_docker.containers.get.return_value = container

        async def ensure(user_id, token):
            calls.append("ensure")

        with patch.object(
            orch,
            "_ensure_skills_current",
            new=AsyncMock(side_effect=ensure),
        ) as mock_ensure, patch.object(
            orch,
            "_wait_healthy",
            new=AsyncMock(),
        ):
            await orch.restart(FAKE_USER["id"])

        mock_ensure.assert_awaited_once_with(FAKE_USER["id"], "tok-restart")
        assert calls == ["ensure", ("restart", 10)]


class TestValidation:
    def test_invalid_network_mode_raises(self, mock_docker, mock_registry, tmp_path):
        from claw_proxy.containers.orchestrator import ContainerOrchestrator

        with pytest.raises(ValueError, match="Invalid ZEROCLAW_NETWORK_MODE"):
            ContainerOrchestrator(
                registry=mock_registry,
                docker_client=mock_docker,
                data_dir=str(tmp_path / "data"),
                network_mode="bridge",
            )


class TestProvisionWithTemplates:
    @pytest.mark.asyncio
    async def test_provision_removes_new_workspace_sentinel_after_late_failure_so_retry_rerenders_token(
        self, mock_docker, mock_registry, tmp_path
    ):
        from claw_proxy.containers.orchestrator import ContainerOrchestrator
        from claw_proxy.containers.workspace import (
            LIFEATLAS_TOOL_TOKEN_PLACEHOLDER,
            WORKSPACE_INIT_SENTINEL,
        )

        templates_dir = tmp_path / "templates"
        data_dir = tmp_path / "data"
        default_dir = templates_dir / "default"
        skill_dir = default_dir / "workspace" / "skills" / "lifeatlas"
        (default_dir / ".zeroclaw").mkdir(parents=True)
        (default_dir / ".zeroclaw" / "config.toml").write_text(
            'workspace_dir = "/zeroclaw-data/workspace"\n',
            encoding="utf-8",
        )
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.toml").write_text(
            f'command = "http://lifeatlas.test/tool?token={LIFEATLAS_TOOL_TOKEN_PLACEHOLDER}"\n',
            encoding="utf-8",
        )

        mock_registry.get = AsyncMock(return_value=None)
        mock_registry.insert = AsyncMock(side_effect=[RuntimeError("db down"), None])

        first_container = MagicMock()
        first_container.id = "container_old"
        first_container.attrs = {
            "NetworkSettings": {"Ports": {"42617/tcp": [{"HostPort": "32768"}]}}
        }
        first_container.reload = MagicMock()
        second_container = MagicMock()
        second_container.id = "container_new"
        second_container.attrs = {
            "NetworkSettings": {"Ports": {"42617/tcp": [{"HostPort": "32769"}]}}
        }
        second_container.reload = MagicMock()
        mock_docker.containers.run.side_effect = [
            None,
            first_container,
            None,
            second_container,
        ]

        orch = ContainerOrchestrator(
            registry=mock_registry,
            docker_client=mock_docker,
            image="zeroclaw:latest",
            data_dir=str(data_dir),
            templates_dir=str(templates_dir),
        )

        volume_dir = data_dir / FAKE_USER["id"]
        sentinel = volume_dir / WORKSPACE_INIT_SENTINEL
        skill_toml = volume_dir / "workspace" / "skills" / "lifeatlas" / "SKILL.toml"

        with patch("claw_proxy.containers.orchestrator.httpx") as mock_httpx, patch(
            "claw_proxy.db.get_profile",
            new=AsyncMock(return_value={}),
        ), patch(
            "claw_proxy.containers.orchestrator.secrets.token_urlsafe",
            side_effect=["tok-old", "tok-new"],
        ):
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_httpx.AsyncClient.return_value = mock_client

            with pytest.raises(RuntimeError, match="db down"):
                await orch.provision(FAKE_USER["id"])

            assert not sentinel.exists()

            info = await orch.provision(FAKE_USER["id"])

        assert info.container_id == "container_new"
        assert sentinel.exists()
        rendered = skill_toml.read_text(encoding="utf-8")
        assert "tok-new" in rendered
        assert "tok-old" not in rendered
        assert LIFEATLAS_TOOL_TOKEN_PLACEHOLDER not in rendered

    @pytest.mark.asyncio
    async def test_provision_copies_default_scaffold_then_named_overrides_and_patches_user_md(
        self, mock_docker, mock_registry, tmp_path
    ):
        from claw_proxy.containers.orchestrator import ContainerOrchestrator
        from claw_proxy.containers.workspace import WORKSPACE_INIT_SENTINEL

        templates_dir = tmp_path / "templates"
        data_dir = tmp_path / "data"
        default_dir = templates_dir / "default"
        alice_dir = templates_dir / "alice"
        default_dir.mkdir(parents=True)
        alice_dir.mkdir(parents=True)
        # Mirror real template layout: workspace files under workspace/
        (default_dir / "workspace").mkdir()
        (default_dir / "workspace" / "IDENTITY.md").write_text("# Default Agent\n")
        (default_dir / "workspace" / "USER.md").write_text(
            "- **Name:** unknown\n- **Date of birth:** unknown\n"
        )
        (default_dir / "workspace" / "MEMORY.md").write_text("# Default Memory\n")
        (default_dir / "workspace" / "skills").mkdir()
        (default_dir / "workspace" / "sessions").mkdir()
        (default_dir / "workspace" / "sessions" / "prefs.md").write_text("default prefs\n")
        (alice_dir / "workspace").mkdir()
        (alice_dir / "workspace" / "IDENTITY.md").write_text("# Alice Agent\n")
        (alice_dir / "workspace" / "USER.md").write_text(
            "- **Name:** unknown\n- **Date of birth:** unknown\n"
        )
        (alice_dir / "workspace" / "SPECIAL.md").write_text("# Alice Special\n")
        (alice_dir / "workspace" / "sessions").mkdir()
        (alice_dir / "workspace" / "sessions" / "prefs.md").write_text("alice prefs\n")

        mock_registry.get = AsyncMock(return_value=None)
        mock_registry.insert = AsyncMock()

        container = MagicMock()
        container.id = "container_abc"
        container.attrs = {
            "NetworkSettings": {"Ports": {"42617/tcp": [{"HostPort": "32768"}]}}
        }
        container.reload = MagicMock()
        mock_docker.containers.run.side_effect = [None, container]

        orch = ContainerOrchestrator(
            registry=mock_registry,
            docker_client=mock_docker,
            image="zeroclaw:latest",
            data_dir=str(data_dir),
            templates_dir=str(templates_dir),
        )

        with patch("claw_proxy.containers.orchestrator.httpx") as mock_httpx, patch(
            "claw_proxy.db.get_profile",
            new=AsyncMock(
                return_value={
                    "first_name": "Alice",
                    "last_name": "Wonderland",
                    "date_of_birth": "1990-01-15",
                }
            ),
        ), patch(
            "claw_proxy.containers.orchestrator.render_lifeatlas_skill_token",
            create=True,
        ) as render_lifeatlas_skill_token:
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_httpx.AsyncClient.return_value = mock_client

            info = await orch.provision(FAKE_USER["id"])

        render_lifeatlas_skill_token.assert_called_once()
        token_volume_path, token = render_lifeatlas_skill_token.call_args.args
        assert token_volume_path.endswith(FAKE_USER["id"])
        assert token == info.bearer_token

        volume_dir = data_dir / FAKE_USER["id"]
        ws = volume_dir / "workspace"
        assert info.container_id == "container_abc"
        assert (ws / "IDENTITY.md").read_text() == "# Alice Agent\n"
        assert (ws / "MEMORY.md").read_text() == "# Default Memory\n"
        assert (ws / "SPECIAL.md").read_text() == "# Alice Special\n"
        assert (ws / "skills").is_dir()
        assert (ws / "sessions").is_dir()
        assert (ws / "temp").is_dir()
        assert (ws / "sessions" / "prefs.md").read_text() == "alice prefs\n"
        assert "- **Name:** Alice Wonderland\n" in (ws / "USER.md").read_text()
        assert "- **Date of birth:** 1990-01-15\n" in (ws / "USER.md").read_text()
        assert (volume_dir / WORKSPACE_INIT_SENTINEL).exists()

    @pytest.mark.asyncio
    async def test_provision_does_not_overwrite_initialized_workspace(
        self, mock_docker, mock_registry, tmp_path
    ):
        from claw_proxy.containers.orchestrator import ContainerOrchestrator
        from claw_proxy.containers.workspace import WORKSPACE_INIT_SENTINEL

        templates_dir = tmp_path / "templates"
        data_dir = tmp_path / "data"
        alice_dir = templates_dir / "alice"
        alice_dir.mkdir(parents=True)
        (alice_dir / "USER.md").write_text(
            "- **Name:** unknown\n- **Date of birth:** unknown\n"
        )

        workspace_dir = data_dir / FAKE_USER["id"]
        workspace_dir.mkdir(parents=True)
        user_md = workspace_dir / "USER.md"
        user_md.write_text("custom content\n")
        (workspace_dir / WORKSPACE_INIT_SENTINEL).write_text("initialized\n")

        mock_registry.get = AsyncMock(return_value=None)
        mock_registry.insert = AsyncMock()

        container = MagicMock()
        container.id = "container_existing"
        container.attrs = {
            "NetworkSettings": {"Ports": {"42617/tcp": [{"HostPort": "32768"}]}}
        }
        container.reload = MagicMock()
        mock_docker.containers.run.side_effect = [None, container]

        orch = ContainerOrchestrator(
            registry=mock_registry,
            docker_client=mock_docker,
            image="zeroclaw:latest",
            data_dir=str(data_dir),
            templates_dir=str(templates_dir),
        )

        with patch("claw_proxy.containers.orchestrator.httpx") as mock_httpx, patch(
            "claw_proxy.db.get_profile",
            new=AsyncMock(
                return_value={
                    "first_name": "Alice",
                    "last_name": "Wonderland",
                    "date_of_birth": "1990-01-15",
                }
            ),
        ), patch(
            "claw_proxy.containers.orchestrator.render_lifeatlas_skill_token",
            create=True,
        ) as render_lifeatlas_skill_token:
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_httpx.AsyncClient.return_value = mock_client

            await orch.provision(FAKE_USER["id"])

        render_lifeatlas_skill_token.assert_not_called()
        assert user_md.read_text() == "custom content\n"
        assert not (workspace_dir / "IDENTITY.md").exists()

    @pytest.mark.asyncio
    async def test_provision_existing_workspace_ensures_skills_current(
        self, mock_docker, mock_registry, tmp_path
    ):
        from claw_proxy.containers.orchestrator import ContainerOrchestrator
        from claw_proxy.containers.workspace import WORKSPACE_INIT_SENTINEL

        templates_dir = tmp_path / "templates"
        (templates_dir / "default").mkdir(parents=True)
        data_dir = tmp_path / "data"
        workspace_dir = data_dir / FAKE_USER["id"]
        workspace_dir.mkdir(parents=True)
        (workspace_dir / WORKSPACE_INIT_SENTINEL).write_text("initialized\n")

        mock_registry.get = AsyncMock(return_value=None)
        mock_registry.insert = AsyncMock()

        container = MagicMock()
        container.id = "container_existing_ws"
        container.attrs = {
            "NetworkSettings": {"Ports": {"42617/tcp": [{"HostPort": "32768"}]}}
        }
        container.reload = MagicMock()
        mock_docker.containers.run.side_effect = [None, container]

        orch = ContainerOrchestrator(
            registry=mock_registry,
            docker_client=mock_docker,
            image="zeroclaw:latest",
            data_dir=str(data_dir),
            templates_dir=str(templates_dir),
        )

        with patch("claw_proxy.containers.orchestrator.httpx") as mock_httpx, patch.object(
            orch,
            "_ensure_skills_current",
            new=AsyncMock(),
        ) as ensure_skills_current:
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_httpx.AsyncClient.return_value = mock_client

            info = await orch.provision(FAKE_USER["id"])

        ensure_skills_current.assert_awaited_once_with(
            FAKE_USER["id"],
            info.bearer_token,
        )


class TestEnsureSkillsCurrent:
    @pytest.mark.asyncio
    async def test_ensure_skills_current_no_op_when_versions_match(self, orch):
        with patch(
            "claw_proxy.containers.orchestrator.read_installed_skill_version",
            return_value="0.2.0",
        ) as read_installed, patch(
            "claw_proxy.containers.orchestrator.read_template_skill_version",
            return_value="0.2.0",
        ) as read_template, patch(
            "claw_proxy.containers.orchestrator.render_lifeatlas_skill",
        ) as render_lifeatlas_skill:
            result = await orch._ensure_skills_current(user_id="u1", token="T")

        read_installed.assert_called_once_with(f"{orch.data_dir}/u1")
        read_template.assert_called_once_with(orch.templates_dir)
        assert result is False
        render_lifeatlas_skill.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_skills_current_renders_when_version_drifts(self, orch):
        orch.host_data_dir = "/host/zeroclaw"
        with patch(
            "claw_proxy.containers.orchestrator.read_installed_skill_version",
            return_value="0.1.0",
        ), patch(
            "claw_proxy.containers.orchestrator.read_template_skill_version",
            return_value="0.2.0",
        ), patch(
            "claw_proxy.containers.orchestrator.render_lifeatlas_skill",
            return_value=3,
        ) as render_lifeatlas_skill:
            result = await orch._ensure_skills_current(user_id="u1", token="T")

        assert result is True
        render_lifeatlas_skill.assert_called_once_with(
            docker_client=orch.docker,
            templates_dir=orch.templates_dir,
            volume_path=f"{orch.data_dir}/u1",
            host_volume_path="/host/zeroclaw/u1",
            token="T",
        )

    @pytest.mark.asyncio
    async def test_ensure_skills_current_no_op_when_installed_is_newer(self, orch):
        with patch(
            "claw_proxy.containers.orchestrator.read_installed_skill_version",
            return_value="0.3.0",
        ), patch(
            "claw_proxy.containers.orchestrator.read_template_skill_version",
            return_value="0.2.0",
        ), patch(
            "claw_proxy.containers.orchestrator.render_lifeatlas_skill",
        ) as render_lifeatlas_skill:
            result = await orch._ensure_skills_current(user_id="u1", token="T")

        assert result is False
        render_lifeatlas_skill.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_skills_current_renders_when_missing_installed(self, orch):
        with patch(
            "claw_proxy.containers.orchestrator.read_installed_skill_version",
            return_value=None,
        ), patch(
            "claw_proxy.containers.orchestrator.read_template_skill_version",
            return_value="0.2.0",
        ), patch(
            "claw_proxy.containers.orchestrator.render_lifeatlas_skill",
            return_value=3,
        ) as render_lifeatlas_skill:
            result = await orch._ensure_skills_current(user_id="u1", token="T")

        assert result is True
        render_lifeatlas_skill.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_skills_current_no_op_when_template_unreadable(self, orch):
        with patch(
            "claw_proxy.containers.orchestrator.read_installed_skill_version",
            return_value="0.1.0",
        ), patch(
            "claw_proxy.containers.orchestrator.read_template_skill_version",
            return_value=None,
        ), patch(
            "claw_proxy.containers.orchestrator.render_lifeatlas_skill",
        ) as render_lifeatlas_skill:
            result = await orch._ensure_skills_current(user_id="u1", token="T")

        assert result is False
        render_lifeatlas_skill.assert_not_called()


class TestEnsureWorkspaceDirectories:
    @pytest.mark.asyncio
    async def test_ensure_workspace_directories_uses_busybox_for_missing_dirs(
        self, mock_docker, mock_registry, tmp_path
    ):
        from claw_proxy.containers.orchestrator import ContainerOrchestrator
        from claw_proxy.containers.workspace import WORKSPACE_INIT_SENTINEL

        data_dir = tmp_path / "data"
        user_dir = data_dir / "user-123"
        user_dir.mkdir(parents=True)
        (user_dir / WORKSPACE_INIT_SENTINEL).write_text("initialized\n")
        (user_dir / "workspace").mkdir()

        orch = ContainerOrchestrator(mock_registry, docker_client=mock_docker, data_dir=str(data_dir))

        await orch.ensure_workspace_directories()

        commands = [
            call.kwargs["command"]
            for call in mock_docker.containers.run.call_args_list
        ]
        assert all(
            command[:4]
            == ["sh", "-ceu", 'mkdir -p -- "$1" && chown 65534:65534 "$1"', "sh"]
            for command in commands
        )
        assert [command[4] for command in commands] == [
            "/vol/workspace/temp",
            "/vol/workspace/lifeatlas/files",
            "/vol/workspace/lifeatlas/photos",
        ]


class TestStop:
    @pytest.mark.asyncio
    async def test_stop(self, orch, mock_docker, mock_registry):
        mock_registry.get = AsyncMock(return_value={
            "container_id": "abc",
            "ws_url": "ws://localhost:32000/ws/chat",
            "http_url": "http://localhost:32000",
            "status": "ready",
            "bearer_token": "tok",
            "current_session_id": None,
        })
        mock_registry.update_status = AsyncMock()
        container = MagicMock()
        mock_docker.containers.get.return_value = container

        await orch.stop(FAKE_USER["id"])
        container.stop.assert_called_once()
        mock_registry.update_status.assert_called_with(FAKE_USER["id"], "stopped")
