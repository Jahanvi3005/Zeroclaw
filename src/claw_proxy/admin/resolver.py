"""Resolve admin lookup strings to user/container records."""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass

from claw_proxy.db import ContainerRegistry


@dataclass(frozen=True)
class ResolutionResult:
    user_id: str
    email: str | None
    first_name: str | None
    last_name: str | None
    container_id: str | None


@dataclass(frozen=True)
class Candidate:
    user_id: str
    email: str | None
    first_name: str | None
    last_name: str | None
    container_id: str | None


class NotFoundError(LookupError):
    pass


class AmbiguousLookupError(LookupError):
    def __init__(self, candidates: list[Candidate]) -> None:
        self.candidates = candidates
        super().__init__(f"Ambiguous lookup: {len(candidates)} candidates")


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _candidate_from_profile(profile: dict, *, container_id: str | None = None) -> Candidate:
    return Candidate(
        user_id=profile.get("id") or profile.get("user_id") or "",
        email=profile.get("email"),
        first_name=profile.get("first_name"),
        last_name=profile.get("last_name"),
        container_id=container_id,
    )


async def _query_profiles(supabase_client, filters: list[tuple[str, str]]) -> list[dict]:
    query = supabase_client.table("decrypted_profiles").select(
        "id,email,first_name,last_name"
    )
    for column, value in filters:
        query = query.eq(column, value)

    response = await _maybe_await(query.execute())
    rows = getattr(response, "data", None)
    if rows is None and isinstance(response, dict):
        rows = response.get("data", [])
    return list(rows or [])


async def _fetch_profile_by_id(supabase_client, user_id: str) -> dict:
    rows = await _query_profiles(supabase_client, [("id", user_id)])
    return rows[0] if rows else {}


async def _finalize_result(
    *,
    user_id: str,
    registry: ContainerRegistry,
    supabase_client,
    profile_seed: dict | None = None,
    registry_seed: dict | None = None,
) -> ResolutionResult:
    profile = await _fetch_profile_by_id(supabase_client, user_id)
    merged_profile = profile if profile else (profile_seed or {})
    registry_row = await registry.get(user_id)
    fallback_registry = registry_row or registry_seed or {}
    container_id = fallback_registry.get("container_id")
    return ResolutionResult(
        user_id=user_id,
        email=merged_profile.get("email"),
        first_name=merged_profile.get("first_name"),
        last_name=merged_profile.get("last_name"),
        container_id=container_id,
    )


async def _resolve_from_registry_rows(
    *,
    rows: list[dict],
    registry: ContainerRegistry,
    supabase_client,
) -> ResolutionResult:
    if not rows:
        raise NotFoundError("No matching container registry row")
    if len(rows) > 1:
        raise AmbiguousLookupError(
            [
                Candidate(
                    user_id=row.get("user_id") or "",
                    email=None,
                    first_name=None,
                    last_name=None,
                    container_id=row.get("container_id"),
                )
                for row in rows
            ]
        )
    row = rows[0]
    user_id = row["user_id"]
    return await _finalize_result(
        user_id=user_id,
        registry=registry,
        supabase_client=supabase_client,
        registry_seed=row,
    )


async def resolve_user(
    lookup: str,
    *,
    registry: ContainerRegistry,
    docker_client,
    supabase_client,
) -> ResolutionResult:
    lookup = lookup.strip()

    try:
        user_id = str(uuid.UUID(lookup))
    except ValueError:
        user_id = ""
    else:
        return await _finalize_result(
            user_id=user_id,
            registry=registry,
            supabase_client=supabase_client,
        )

    if "@" in lookup:
        rows = await _query_profiles(supabase_client, [("email", lookup)])
        if not rows:
            raise NotFoundError(f"No user found for {lookup!r}")
        if len(rows) > 1:
            raise AmbiguousLookupError([_candidate_from_profile(row) for row in rows])
        profile = rows[0]
        return await _finalize_result(
            user_id=profile["id"],
            registry=registry,
            supabase_client=supabase_client,
            profile_seed=profile,
        )

    if lookup.startswith("zeroclaw-"):
        prefix = lookup.removeprefix("zeroclaw-")
        if prefix:
            rows = await registry.list_all()
            matches = [row for row in rows if (row.get("user_id") or "").startswith(prefix)]
            if matches:
                return await _resolve_from_registry_rows(
                    rows=matches,
                    registry=registry,
                    supabase_client=supabase_client,
                )

    rows = await registry.list_all()
    container_matches = [row for row in rows if (row.get("container_id") or "").startswith(lookup)]
    if container_matches:
        return await _resolve_from_registry_rows(
            rows=container_matches,
            registry=registry,
            supabase_client=supabase_client,
        )

    parts = lookup.split(maxsplit=1)
    if len(parts) == 2:
        first_name, last_name = parts
        rows = await _query_profiles(
            supabase_client,
            [("first_name", first_name), ("last_name", last_name)],
        )
        if rows:
            if len(rows) > 1:
                raise AmbiguousLookupError([_candidate_from_profile(row) for row in rows])
            profile = rows[0]
            return await _finalize_result(
                user_id=profile["id"],
                registry=registry,
                supabase_client=supabase_client,
                profile_seed=profile,
            )

    raise NotFoundError(f"No user found for {lookup!r}")
