from __future__ import annotations

import json

import httpx
from cryptography.fernet import Fernet, InvalidToken


class StoreUnavailable(Exception):
    pass


class RemoteStore:
    def __init__(self, client: httpx.AsyncClient, cipher: Fernet):
        self.client = client
        self.cipher = cipher

    async def _request(self, owner: str, kind: str, operation: str, **fields) -> dict:
        try:
            response = await self.client.post(
                "/api/internal/telegram-state",
                json={"owner": owner, "kind": kind, "operation": operation, **fields},
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise StoreUnavailable
            return result
        except (httpx.HTTPError, ValueError) as error:
            raise StoreUnavailable from error

    def _open(self, owner: str, kind: str, item_id: str, value: str) -> dict:
        try:
            content = json.loads(self.cipher.decrypt(value))
            if content["owner"] != owner or content["kind"] != kind or content["item"]["id"] != item_id:
                raise StoreUnavailable
            return content["item"]
        except (InvalidToken, TypeError, KeyError, ValueError) as error:
            raise StoreUnavailable from error

    async def get(self, owner: str, kind: str, item_id: str) -> dict | None:
        result = await self._request(owner, kind, "get", id=item_id)
        value = result.get("value")
        return self._open(owner, kind, item_id, value) if value is not None else None

    async def list(self, owner: str, kind: str) -> list[dict]:
        items = []
        cursor = None
        while True:
            page = await self._request(owner, kind, "list", cursor=cursor)
            for record in page["items"]:
                items.append(self._open(owner, kind, record["id"], record["value"]))
            next_cursor = page.get("cursor")
            if not next_cursor:
                return items
            if next_cursor == cursor:
                raise StoreUnavailable
            cursor = next_cursor

    async def save(self, owner: str, kind: str, item: dict) -> None:
        document = json.dumps({"owner": owner, "kind": kind, "item": item})
        value = self.cipher.encrypt(document.encode()).decode()
        await self._request(owner, kind, "save", id=item["id"], value=value)

    async def remove(self, owner: str, kind: str, item_id: str) -> None:
        await self._request(owner, kind, "remove", id=item_id)
