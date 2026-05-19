"""Container registry — CRUD operations via Supabase REST API."""

import logging

from claw_proxy.config import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL, http_client
from claw_proxy.crypto import TokenCrypto

log = logging.getLogger(__name__)


def _supa_headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }


async def get_profile(user_id: str) -> dict:
    """Fetch user profile data from the decrypted_profiles view."""
    resp = await http_client.get(
        f"{SUPABASE_URL}/rest/v1/decrypted_profiles"
        f"?id=eq.{user_id}&select=first_name,last_name,date_of_birth",
        headers=_supa_headers(),
    )
    if resp.status_code != 200 or not resp.json():
        return {}
    return resp.json()[0]


class ContainerRegistry:
    def __init__(self, encryption_key: str) -> None:
        self.crypto = TokenCrypto(encryption_key)

    async def list_all(self) -> list[dict]:
        """Return all registry rows."""
        resp = await http_client.get(
            f"{SUPABASE_URL}/rest/v1/container_registry"
            f"?select=user_id,container_id,ws_url,http_url,bearer_token_enc,status,current_session_id",
            headers=_supa_headers(),
        )
        if resp.status_code != 200:
            log.error("container_registry list failed: %s", resp.text)
            raise RuntimeError("Database unavailable")
        rows = resp.json()
        results: list[dict] = []
        for row in rows:
            item = dict(row)
            bearer_token_enc = item.pop("bearer_token_enc", None)
            if bearer_token_enc is not None:
                item["bearer_token"] = self.crypto.decrypt(bearer_token_enc)
            results.append(item)
        return results

    async def get(self, user_id: str) -> dict | None:
        """Look up container for a user. Returns None if not found.

        Decrypts bearer_token_enc → bearer_token in the returned dict.
        """
        resp = await http_client.get(
            f"{SUPABASE_URL}/rest/v1/container_registry"
            f"?user_id=eq.{user_id}"
            f"&select=container_id,ws_url,http_url,bearer_token_enc,status,current_session_id",
            headers=_supa_headers(),
        )
        if resp.status_code != 200:
            log.error("container_registry query failed: %s", resp.text)
            raise RuntimeError("Database unavailable")
        rows = resp.json()
        if not rows:
            return None
        row = rows[0]
        row["bearer_token"] = self.crypto.decrypt(row.pop("bearer_token_enc"))
        return row

    async def insert(
        self,
        user_id: str,
        container_id: str,
        ws_url: str,
        http_url: str,
        bearer_token: str,
        status: str = "provisioning",
    ) -> None:
        resp = await http_client.post(
            f"{SUPABASE_URL}/rest/v1/container_registry",
            headers={**_supa_headers(), "Content-Type": "application/json"},
            json={
                "user_id": user_id,
                "container_id": container_id,
                "ws_url": ws_url,
                "http_url": http_url,
                "bearer_token_enc": self.crypto.encrypt(bearer_token),
                "status": status,
            },
        )
        if resp.status_code not in (200, 201):
            log.error("container_registry insert failed: %s", resp.text)
            raise RuntimeError("Failed to save container mapping")

    async def update_urls(self, user_id: str, ws_url: str, http_url: str) -> None:
        resp = await http_client.patch(
            f"{SUPABASE_URL}/rest/v1/container_registry?user_id=eq.{user_id}",
            headers={**_supa_headers(), "Content-Type": "application/json"},
            json={"ws_url": ws_url, "http_url": http_url},
        )
        if resp.status_code not in (200, 204):
            log.error("container_registry URL update failed: %s", resp.text)
            raise RuntimeError("Failed to update container URLs")

    async def update_status(self, user_id: str, status: str) -> None:
        resp = await http_client.patch(
            f"{SUPABASE_URL}/rest/v1/container_registry?user_id=eq.{user_id}",
            headers={**_supa_headers(), "Content-Type": "application/json"},
            json={"status": status},
        )
        if resp.status_code not in (200, 204):
            log.error("container_registry status update failed: %s", resp.text)
            raise RuntimeError("Failed to update container status")

    async def update_activity(self, user_id: str) -> None:
        from datetime import datetime, timezone

        resp = await http_client.patch(
            f"{SUPABASE_URL}/rest/v1/container_registry?user_id=eq.{user_id}",
            headers={**_supa_headers(), "Content-Type": "application/json"},
            json={"last_active_at": datetime.now(timezone.utc).isoformat()},
        )
        if resp.status_code not in (200, 204):
            log.warning("Failed to update last_active_at: %s", resp.text)

    async def update_session_id(self, user_id: str, session_id: str | None) -> None:
        resp = await http_client.patch(
            f"{SUPABASE_URL}/rest/v1/container_registry?user_id=eq.{user_id}",
            headers={**_supa_headers(), "Content-Type": "application/json"},
            json={"current_session_id": session_id},
        )
        if resp.status_code not in (200, 204):
            log.warning("Failed to update current_session_id: %s", resp.text)

    async def delete(self, user_id: str) -> None:
        resp = await http_client.delete(
            f"{SUPABASE_URL}/rest/v1/container_registry?user_id=eq.{user_id}",
            headers=_supa_headers(),
        )
        if resp.status_code not in (200, 204):
            log.error("container_registry delete failed: %s", resp.text)
            raise RuntimeError("Failed to delete container mapping")
