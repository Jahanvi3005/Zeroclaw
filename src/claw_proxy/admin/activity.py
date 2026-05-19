"""Activity checking helpers for admin careful-mode decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol


@dataclass
class ActivityState:
    is_idle: bool
    last_activity_at: datetime | None
    signals: list[str] = field(default_factory=list)


class ActivityChecker(Protocol):
    async def get_state(self, user_id: str) -> ActivityState: ...


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class DefaultActivityChecker:
    def __init__(
        self,
        registry,
        get_connections: Callable[[], dict[str, set]],
        idle_threshold_seconds: int = 60,
    ) -> None:
        self.registry = registry
        self.get_connections = get_connections
        self.idle_threshold_seconds = idle_threshold_seconds

    async def get_state(self, user_id: str) -> ActivityState:
        signals: list[str] = []
        is_idle = True

        connections = self.get_connections().get(user_id, set())
        if connections:
            is_idle = False
            count = len(connections)
            signals.append(f"{count} active WS tab" + ("s" if count != 1 else ""))

        row = await self.registry.get(user_id)
        last_activity_at = _parse_iso_timestamp(
            row.get("last_active_at") if row is not None else None
        )
        if last_activity_at is None:
            is_idle = False
            signals.append("no last_active_at")
        else:
            age_seconds = (datetime.now(timezone.utc) - last_activity_at).total_seconds()
            if age_seconds <= self.idle_threshold_seconds:
                is_idle = False
                signals.append(f"last activity {int(age_seconds)} seconds ago")

        return ActivityState(
            is_idle=is_idle,
            last_activity_at=last_activity_at,
            signals=signals,
        )
