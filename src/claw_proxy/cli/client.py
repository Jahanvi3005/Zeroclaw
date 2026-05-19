"""HTTP client wrapper for the admin REST API."""

from __future__ import annotations

import httpx


class AdminApiClient:
    DEFAULT_TIMEOUT_SECONDS = 120.0

    def __init__(
        self,
        *,
        api_url: str,
        token: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.token = token
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _url(self, path: str) -> str:
        return f"{self.api_url}/claw-admin{path}"

    async def get(self, path: str, *, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self.DEFAULT_TIMEOUT_SECONDS,
        ) as client:
            response = await client.get(self._url(path), params=params, headers=self._headers())
            response.raise_for_status()
            return response.json()

    async def post(self, path: str, *, json: dict | None = None) -> dict:
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self.DEFAULT_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post(self._url(path), json=json, headers=self._headers())
            response.raise_for_status()
            return response.json()

    async def put(self, path: str, *, json: dict | None = None) -> dict:
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self.DEFAULT_TIMEOUT_SECONDS,
        ) as client:
            response = await client.put(self._url(path), json=json, headers=self._headers())
            response.raise_for_status()
            return response.json()

    async def patch(self, path: str, *, json: dict | None = None) -> dict:
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self.DEFAULT_TIMEOUT_SECONDS,
        ) as client:
            response = await client.patch(self._url(path), json=json, headers=self._headers())
            response.raise_for_status()
            return response.json()

    async def delete(self, path: str) -> dict:
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self.DEFAULT_TIMEOUT_SECONDS,
        ) as client:
            response = await client.delete(self._url(path), headers=self._headers())
            response.raise_for_status()
            return response.json()
