from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import FAKE_ENCRYPTION_KEY, FAKE_USER


@pytest.fixture
def registry():
    from claw_proxy.db import ContainerRegistry

    return ContainerRegistry(FAKE_ENCRYPTION_KEY)


@pytest.fixture
def docker_client():
    return MagicMock()


@pytest.fixture
def supabase_client():
    client = MagicMock()
    query = MagicMock()
    query.eq.return_value = query
    query.execute = AsyncMock(return_value=MagicMock(data=[]))
    client.table.return_value.select.return_value = query
    return client


def _supabase_rows(client, rows):
    query = client.table.return_value.select.return_value
    query.execute = AsyncMock(return_value=MagicMock(data=rows))


class TestResolveUser:
    @pytest.mark.asyncio
    async def test_uuid_passthrough_fetches_profile_and_registry(
        self, registry, docker_client, supabase_client
    ):
        from claw_proxy.admin.resolver import ResolutionResult, resolve_user

        registry.get = AsyncMock(
            return_value={
                "container_id": "container_abc",
                "ws_url": "ws://localhost:9000/ws/chat",
                "http_url": "http://localhost:9000",
                "bearer_token": "tok",
                "status": "ready",
                "current_session_id": None,
            }
        )
        _supabase_rows(
            supabase_client,
            [
                {
                    "id": FAKE_USER["id"],
                    "email": FAKE_USER["email"],
                    "first_name": "Alice",
                    "last_name": "Smith",
                }
            ],
        )

        result = await resolve_user(
            FAKE_USER["id"],
            registry=registry,
            docker_client=docker_client,
            supabase_client=supabase_client,
        )

        assert result == ResolutionResult(
            user_id=FAKE_USER["id"],
            email=FAKE_USER["email"],
            first_name="Alice",
            last_name="Smith",
            container_id="container_abc",
        )
        supabase_client.table.assert_called_with("decrypted_profiles")
        registry.get.assert_awaited_once_with(FAKE_USER["id"])

    @pytest.mark.asyncio
    async def test_email_lookup(self, registry, docker_client, supabase_client):
        from claw_proxy.admin.resolver import ResolutionResult, resolve_user

        registry.get = AsyncMock(
            return_value={
                "container_id": "container_abc",
                "ws_url": "ws://localhost:9000/ws/chat",
                "http_url": "http://localhost:9000",
                "bearer_token": "tok",
                "status": "ready",
                "current_session_id": None,
            }
        )
        _supabase_rows(
            supabase_client,
            [
                {
                    "id": FAKE_USER["id"],
                    "email": FAKE_USER["email"],
                    "first_name": "Alice",
                    "last_name": "Smith",
                }
            ],
        )

        result = await resolve_user(
            FAKE_USER["email"],
            registry=registry,
            docker_client=docker_client,
            supabase_client=supabase_client,
        )

        assert result == ResolutionResult(
            user_id=FAKE_USER["id"],
            email=FAKE_USER["email"],
            first_name="Alice",
            last_name="Smith",
            container_id="container_abc",
        )

    @pytest.mark.asyncio
    async def test_first_last_name_ambiguous(self, registry, docker_client, supabase_client):
        from claw_proxy.admin.resolver import AmbiguousLookupError, Candidate, resolve_user

        registry.list_all = AsyncMock(return_value=[])
        _supabase_rows(
            supabase_client,
            [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "email": "a@example.com",
                    "first_name": "Alice",
                    "last_name": "Smith",
                },
                {
                    "id": "22222222-2222-2222-2222-222222222222",
                    "email": "b@example.com",
                    "first_name": "Alice",
                    "last_name": "Smith",
                },
            ],
        )

        with pytest.raises(AmbiguousLookupError) as exc:
            await resolve_user(
                "Alice Smith",
                registry=registry,
                docker_client=docker_client,
                supabase_client=supabase_client,
            )

        assert exc.value.candidates == [
            Candidate(
                user_id="11111111-1111-1111-1111-111111111111",
                email="a@example.com",
                first_name="Alice",
                last_name="Smith",
                container_id=None,
            ),
            Candidate(
                user_id="22222222-2222-2222-2222-222222222222",
                email="b@example.com",
                first_name="Alice",
                last_name="Smith",
                container_id=None,
            ),
        ]

    @pytest.mark.asyncio
    async def test_container_name_lookup(self, registry, docker_client, supabase_client):
        from claw_proxy.admin.resolver import ResolutionResult, resolve_user

        registry.list_all = AsyncMock(
            return_value=[
                {
                    "user_id": FAKE_USER["id"],
                    "container_id": "container_abc",
                    "ws_url": "ws://localhost:9000/ws/chat",
                    "http_url": "http://localhost:9000",
                    "bearer_token_enc": "enc",
                    "status": "ready",
                    "current_session_id": None,
                }
            ]
        )
        registry.get = AsyncMock(
            return_value={
                "container_id": "container_abc",
                "ws_url": "ws://localhost:9000/ws/chat",
                "http_url": "http://localhost:9000",
                "bearer_token": "tok",
                "status": "ready",
                "current_session_id": None,
            }
        )
        _supabase_rows(
            supabase_client,
            [
                {
                    "id": FAKE_USER["id"],
                    "email": FAKE_USER["email"],
                    "first_name": "Alice",
                    "last_name": "Smith",
                }
            ],
        )
        result = await resolve_user(
            f"zeroclaw-{FAKE_USER['id'][:8]}",
            registry=registry,
            docker_client=docker_client,
            supabase_client=supabase_client,
        )

        assert result == ResolutionResult(
            user_id=FAKE_USER["id"],
            email=FAKE_USER["email"],
            first_name="Alice",
            last_name="Smith",
            container_id="container_abc",
        )
        supabase_client.table.assert_called_with("decrypted_profiles")

    @pytest.mark.asyncio
    async def test_container_id_prefix_beats_name_lookup(self, registry, docker_client):
        from claw_proxy.admin.resolver import ResolutionResult, resolve_user

        container_user_id = "33333333-3333-3333-3333-333333333333"
        name_user_id = "44444444-4444-4444-4444-444444444444"

        registry.list_all = AsyncMock(
            return_value=[
                {
                    "user_id": container_user_id,
                    "container_id": "Alice Smith-container-001",
                    "ws_url": "ws://localhost:9000/ws/chat",
                    "http_url": "http://localhost:9000",
                    "bearer_token_enc": "enc",
                    "status": "ready",
                    "current_session_id": None,
                }
            ]
        )
        registry.get = AsyncMock(
            return_value={
                "container_id": "Alice Smith-container-001",
                "ws_url": "ws://localhost:9000/ws/chat",
                "http_url": "http://localhost:9000",
                "bearer_token": "tok",
                "status": "ready",
                "current_session_id": None,
            }
        )

        supabase_client = MagicMock()

        def make_query():
            filters: list[tuple[str, str]] = []
            query = MagicMock()

            def add_filter(column, value):
                filters.append((column, value))
                return query

            async def execute():
                if filters == [("first_name", "Alice"), ("last_name", "Smith")]:
                    return MagicMock(
                        data=[
                            {
                                "id": name_user_id,
                                "email": "name@example.com",
                                "first_name": "Alice",
                                "last_name": "Smith",
                            }
                        ]
                    )
                if filters == [("id", container_user_id)]:
                    return MagicMock(
                        data=[
                            {
                                "id": container_user_id,
                                "email": "container@example.com",
                                "first_name": "Container",
                                "last_name": "User",
                            }
                        ]
                    )
                return MagicMock(data=[])

            query.eq.side_effect = add_filter
            query.execute = AsyncMock(side_effect=execute)
            return query

        supabase_client.table.return_value.select.side_effect = lambda *args, **kwargs: make_query()

        result = await resolve_user(
            "Alice Smith",
            registry=registry,
            docker_client=docker_client,
            supabase_client=supabase_client,
        )

        assert result == ResolutionResult(
            user_id=container_user_id,
            email="container@example.com",
            first_name="Container",
            last_name="User",
            container_id="Alice Smith-container-001",
        )
