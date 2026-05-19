"""Structured 501 stubs for container update flows."""

from __future__ import annotations

from pydantic import BaseModel


class UpdateBody(BaseModel):
    mode: str
    image_tag: str | None = None


class BatchUpdateBody(BaseModel):
    targets: list[str]
    mode: str = "in_place"
    image_tag: str | None = None


UPDATE_NOT_IMPLEMENTED = {
    "error": "update_not_implemented",
    "planned_modes": ["in_place", "image_swap"],
}
