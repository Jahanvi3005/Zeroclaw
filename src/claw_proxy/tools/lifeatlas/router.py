"""FastAPI router exposing LifeAtlas helpers as ZeroClaw HTTP tools."""

from __future__ import annotations

import asyncio
import logging
import uuid
from functools import partial
from typing import Any

from fastapi import APIRouter, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from claw_proxy.files.workspace_fetch import fetch_to_workspace as _fetch_to_workspace
from claw_proxy.tools.lifeatlas.common import (
    ContainerNotProvisioned,
    FetchTooLarge,
    StorageFetchError,
    ToolLookupError,
    parse_bool_param,
    parse_int_param,
    resolve_active_profile_id,
)
from claw_proxy.tools.lifeatlas.events import EventsHelper
from claw_proxy.tools.lifeatlas.files import FilesHelper
from claw_proxy.tools.lifeatlas.health_context import HealthContextHelper
from claw_proxy.tools.lifeatlas.log_events import LogEventsHelper
from claw_proxy.tools.lifeatlas.medicines import MedicinesHelper
from claw_proxy.tools.lifeatlas.oura import OuraHelper
from claw_proxy.tools.lifeatlas.save import SaveHelper
from claw_proxy.tools.lifeatlas.strava import StravaHelper
from claw_proxy.tools.lifeatlas.timeline import TimelineHelper
from claw_proxy.tools.lifeatlas.training import TrainingHelper
from claw_proxy.tools.lifeatlas.whoop import WhoopHelper

log = logging.getLogger(__name__)


class UnknownToolError(Exception):
    """Raised when a requested LifeAtlas tool name is not registered."""


class ToolParameterError(ValueError):
    """Raised for client-supplied tool query parameter validation errors."""


def _parse_int_param(*args, **kwargs) -> int:
    try:
        return parse_int_param(*args, **kwargs)
    except ValueError as exc:
        raise ToolParameterError(str(exc)) from exc


def _parse_bool_param(*args, **kwargs) -> bool:
    try:
        return parse_bool_param(*args, **kwargs)
    except ValueError as exc:
        raise ToolParameterError(str(exc)) from exc


def _require_uuid(params: dict, name: str) -> str:
    raw = params.get(name)
    if raw is None or raw == "":
        raise ToolParameterError(f"{name} is required")
    try:
        parsed = uuid.UUID(raw)
    except (TypeError, ValueError) as exc:
        raise ToolParameterError(f"{name} must be a UUID") from exc
    return str(parsed)


def _optional_uuid(params: dict, name: str) -> str | None:
    raw = params.get(name)
    if not raw:
        return raw
    try:
        parsed = uuid.UUID(raw)
    except (TypeError, ValueError) as exc:
        raise ToolParameterError(f"{name} must be a UUID") from exc
    return str(parsed)


def _profile_id_for(supabase, user_id: str) -> str:
    return resolve_active_profile_id(supabase, user_id)


async def _call_tool(
    supabase,
    orchestrator,
    user_id: str,
    tool_name: str,
    params: dict[str, str | None],
) -> dict[str, Any]:
    strava = StravaHelper(supabase)
    oura = OuraHelper(supabase)
    whoop = WhoopHelper(supabase)
    timeline = TimelineHelper(supabase)
    medicines = MedicinesHelper(supabase)
    training = TrainingHelper(supabase)
    events = EventsHelper(supabase)
    health_context = HealthContextHelper(supabase)
    files_fetch = partial(
        _fetch_to_workspace,
        orchestrator=orchestrator,
        supabase=supabase,
    )
    files = FilesHelper(supabase, files_fetch)
    log_events = LogEventsHelper(supabase, files_fetch)
    save = SaveHelper(supabase, orchestrator)

    def get_days() -> int:
        return _parse_int_param(
            params.get("days"),
            default=7,
            minimum=1,
            maximum=90,
            name="days",
        )

    def get_timeline_limit() -> int:
        return _parse_int_param(
            params.get("limit"),
            default=50,
            minimum=1,
            maximum=100,
            name="limit",
        )

    def get_events_limit() -> int:
        return _parse_int_param(
            params.get("limit"),
            default=20,
            minimum=1,
            maximum=100,
            name="limit",
        )

    def get_weeks_ago() -> int:
        return _parse_int_param(
            params.get("weeks_ago"),
            default=0,
            minimum=0,
            maximum=52,
            name="weeks_ago",
        )

    def get_active_only() -> bool:
        return _parse_bool_param(
            params.get("active_only"),
            default=True,
            name="active_only",
        )

    def get_upcoming_only() -> bool:
        return _parse_bool_param(
            params.get("upcoming_only"),
            default=True,
            name="upcoming_only",
        )

    if tool_name == "get_weekly_training":
        return await asyncio.to_thread(
            strava.get_weekly_summary,
            user_id,
            get_weeks_ago(),
        )
    if tool_name == "get_last_workout":
        return await asyncio.to_thread(strava.get_recent_activity, user_id) or {
            "message": "No recent activities found"
        }
    if tool_name == "get_recovery_status":
        return await asyncio.to_thread(oura.get_recovery_status, user_id)
    if tool_name == "get_recent_sleep":
        return await asyncio.to_thread(oura.get_recent_sleep, user_id, get_days())
    if tool_name == "get_recent_readiness":
        return await asyncio.to_thread(oura.get_recent_readiness, user_id, get_days())
    if tool_name == "get_heart_rate_trends":
        return await asyncio.to_thread(oura.get_heart_rate_trends, user_id, get_days())
    if tool_name == "get_whoop_recovery_status":
        return await asyncio.to_thread(whoop.get_recovery_status, user_id)
    if tool_name == "get_whoop_recent_recovery":
        return await asyncio.to_thread(whoop.get_recent_recovery, user_id, get_days())
    if tool_name == "get_whoop_recent_sleep":
        return await asyncio.to_thread(whoop.get_recent_sleep, user_id, get_days())
    if tool_name == "get_whoop_recent_strain":
        return await asyncio.to_thread(whoop.get_recent_strain, user_id, get_days())
    if tool_name == "get_whoop_heart_rate_trends":
        return await asyncio.to_thread(whoop.get_heart_rate_trends, user_id, get_days())
    if tool_name == "get_timeline_entry_types":
        profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
        return await asyncio.to_thread(timeline.get_entry_types, user_id, profile_id)
    if tool_name == "get_timeline_entries":
        profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
        return await asyncio.to_thread(
            timeline.get_timeline_entries,
            user_id,
            profile_id,
            get_timeline_limit(),
            params.get("entry_type"),
        )
    if tool_name == "get_injuries":
        profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
        return await asyncio.to_thread(
            timeline.get_injuries,
            user_id,
            profile_id,
            get_active_only(),
        )
    if tool_name == "get_current_medicines":
        profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
        return await asyncio.to_thread(
            medicines.get_current_medicines,
            user_id,
            profile_id,
        )
    if tool_name == "get_active_training_plan":
        return await asyncio.to_thread(training.get_active_plan, user_id)
    if tool_name == "get_upcoming_sessions":
        return await asyncio.to_thread(
            training.get_upcoming_sessions,
            user_id,
            get_days(),
        )
    if tool_name == "get_recent_load_metrics":
        return await asyncio.to_thread(
            training.get_recent_load_metrics,
            user_id,
            get_days(),
        )
    if tool_name == "get_user_events":
        return await asyncio.to_thread(
            events.get_user_events,
            user_id,
            get_upcoming_only(),
            get_events_limit(),
        )
    if tool_name == "get_selected_races":
        return await asyncio.to_thread(events.get_selected_races, user_id)
    if tool_name == "get_healthcare_summary":
        profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
        return await asyncio.to_thread(
            health_context.get_healthcare_summary,
            user_id,
            profile_id,
        )
    if tool_name == "get_conditions":
        profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
        return await asyncio.to_thread(
            health_context.get_conditions,
            user_id,
            profile_id,
        )
    if tool_name == "get_bmr_summary":
        profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
        return await asyncio.to_thread(
            health_context.get_bmr_summary,
            user_id,
            profile_id,
        )
    if tool_name == "get_life_balance_summary":
        profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
        return await asyncio.to_thread(
            health_context.get_life_balance_summary,
            user_id,
            profile_id,
        )
    if tool_name == "list_health_data_files":
        timeline_entry_id = _optional_uuid(params, "timeline_entry_id")
        profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
        return await asyncio.to_thread(
            files.list_health_data_files,
            user_id,
            profile_id,
            timeline_entry_id,
        )
    if tool_name == "get_health_data_file_content":
        file_id = _require_uuid(params, "file_id")
        profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
        full = _parse_bool_param(params.get("full"), default=False, name="full")
        return await files.get_health_data_file_content(
            user_id,
            profile_id,
            file_id,
            full,
        )
    if tool_name == "list_log_events":
        profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
        return await asyncio.to_thread(
            log_events.list_log_events,
            user_id,
            profile_id,
            params.get("event_type"),
            _parse_int_param(
                params.get("days"),
                default=30,
                minimum=1,
                maximum=365,
                name="days",
            ),
            _parse_int_param(
                params.get("limit"),
                default=50,
                minimum=1,
                maximum=200,
                name="limit",
            ),
        )
    if tool_name == "get_log_event_photo":
        event_id = _require_uuid(params, "event_id")
        profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
        return await log_events.get_log_event_photo(user_id, profile_id, event_id)
    if tool_name == "save_to_library":
        return await save.save_to_library(user_id, params.get("workspace_path"))

    raise UnknownToolError(tool_name)


def create_lifeatlas_tools_router(
    token_map: dict[str, str],
    get_supabase_client,
    get_orchestrator,
) -> APIRouter:
    router = APIRouter()

    @router.get("/tools/lifeatlas/{tool_name}")
    async def lifeatlas_tool(
        tool_name: str,
        token: str | None = Query(default=None),
        days: str | None = Query(default=None),
        limit: str | None = Query(default=None),
        weeks_ago: str | None = Query(default=None),
        active_only: str | None = Query(default=None),
        upcoming_only: str | None = Query(default=None),
        entry_type: str | None = Query(default=None),
        file_id: str | None = Query(default=None),
        event_id: str | None = Query(default=None),
        timeline_entry_id: str | None = Query(default=None),
        full: str | None = Query(default=None),
        workspace_path: str | None = Query(default=None),
        event_type: str | None = Query(default=None),
    ):
        if not token:
            return JSONResponse(status_code=401, content={"error": "missing token"})

        user_id = token_map.get(token)
        if not user_id:
            return JSONResponse(status_code=401, content={"error": "unknown token"})

        params = {
            "days": days,
            "limit": limit,
            "weeks_ago": weeks_ago,
            "active_only": active_only,
            "upcoming_only": upcoming_only,
            "entry_type": entry_type,
            "file_id": file_id,
            "event_id": event_id,
            "timeline_entry_id": timeline_entry_id,
            "full": full,
            "workspace_path": workspace_path,
            "event_type": event_type,
        }

        try:
            supabase = get_supabase_client()
            orchestrator = get_orchestrator()
            result = await _call_tool(
                supabase,
                orchestrator,
                user_id,
                tool_name,
                params,
            )
            return JSONResponse(status_code=200, content=jsonable_encoder(result))
        except UnknownToolError:
            return JSONResponse(status_code=404, content={"error": "unknown tool"})
        except ToolParameterError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        except ToolLookupError as exc:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "detail": str(exc)},
            )
        except ContainerNotProvisioned:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "container_not_provisioned",
                    "detail": "Container not yet ready; try again shortly.",
                },
            )
        except FetchTooLarge as exc:
            return JSONResponse(
                status_code=413,
                content={
                    "error": "file_too_large",
                    "detail": (
                        f"File is {exc.size_bytes} bytes; "
                        "the fetch limit is 25 MB."
                    ),
                },
            )
        except StorageFetchError:
            return JSONResponse(
                status_code=502,
                content={"error": "storage_unavailable"},
            )
        except ValueError as exc:
            if "active profile" in str(exc):
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "no_active_profile",
                        "detail": "No active profile is configured for this user.",
                    },
                )
            log.warning(
                "LifeAtlas tool %s failed for user %s",
                tool_name,
                user_id[:8],
                exc_info=True,
            )
            return JSONResponse(
                status_code=502,
                content={"error": "tool unavailable"},
            )
        except Exception as exc:
            log.warning(
                "LifeAtlas tool %s failed for user %s",
                tool_name,
                user_id[:8],
                exc_info=True,
            )
            return JSONResponse(
                status_code=502,
                content={"error": "tool unavailable"},
            )

    return router
