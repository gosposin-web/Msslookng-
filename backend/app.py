from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx

from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from remote_store import RemoteStore, StoreUnavailable
from telegram_client import LoginState, Proxy, TelegramClient, TelegramProblem, normalize_proxy_host


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str = field(repr=False)
    token: str = field(repr=False)
    cipher: Fernet = field(repr=False)
    data_dir: Path
    state_origin: str = ""
    state_access: str = field(default="", repr=False)
    state_token: str = field(default="", repr=False)

    @classmethod
    def from_environment(cls):
        api_id = os.environ.get("TELEGRAM_API_ID", "")
        api_hash = os.environ.get("TELEGRAM_API_HASH", "")
        token = os.environ.get("PANEL_BACKEND_TOKEN", "")
        if (api_id or api_hash) and (
            not api_id.isdigit() or int(api_id) < 1 or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash)
        ):
            raise RuntimeError("Configure TELEGRAM_API_ID and TELEGRAM_API_HASH.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
            raise RuntimeError("Configure PANEL_BACKEND_TOKEN as a long random secret.")
        try:
            cipher = Fernet(os.environ.get("PANEL_STATE_KEY", ""))
        except (TypeError, ValueError) as error:
            raise RuntimeError("Configure PANEL_STATE_KEY with a valid encryption key.") from error
        state_origin = os.environ.get("PANEL_STATE_ORIGIN", "")
        state_access = os.environ.get("PANEL_STATE_ACCESS", "")
        state_token = os.environ.get("PANEL_STATE_TOKEN", "")
        if any((state_origin, state_access, state_token)):
            origin = urlsplit(state_origin)
            if (
                origin.scheme != "https" or not origin.hostname or origin.username or origin.password
                or origin.path not in {"", "/"} or origin.query or origin.fragment
                or not state_access or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", state_token)
            ):
                raise RuntimeError("Configure the remote storage origin and service credentials.")
        elif os.environ.get("RENDER") == "true":
            raise RuntimeError("Configure persistent remote storage before deploying on Render.")
        return cls(
            int(api_id or 0), api_hash, token, cipher, Path(os.environ.get("APP_DATA_DIR", "data")),
            state_origin, state_access, state_token,
        )


class Store:
    def __init__(self, settings: Settings):
        settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = settings.data_dir / "panel.sqlite3"
        self.cipher = settings.cipher
        with self.connect() as database:
            database.execute("""
                CREATE TABLE IF NOT EXISTS items (
                    owner TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    id TEXT NOT NULL,
                    value BLOB NOT NULL,
                    PRIMARY KEY (owner, kind, id)
                )
            """)

    @contextmanager
    def connect(self):
        database = sqlite3.connect(self.path, timeout=3)
        try:
            with database:
                yield database
        finally:
            database.close()

    async def get(self, owner: str, kind: str, item_id: str) -> dict | None:
        with self.connect() as database:
            row = database.execute(
                "SELECT value FROM items WHERE owner = ? AND kind = ? AND id = ?", (owner, kind, item_id)
            ).fetchone()
        return json.loads(self.cipher.decrypt(row[0])) if row else None

    async def list(self, owner: str, kind: str) -> list[dict]:
        with self.connect() as database:
            rows = database.execute(
                "SELECT value FROM items WHERE owner = ? AND kind = ? ORDER BY rowid", (owner, kind)
            ).fetchall()
        return [json.loads(self.cipher.decrypt(row[0])) for row in rows]

    async def save(self, owner: str, kind: str, item: dict) -> None:
        value = self.cipher.encrypt(json.dumps(item).encode())
        with self.connect() as database:
            database.execute("""
                INSERT INTO items (owner, kind, id, value) VALUES (?, ?, ?, ?)
                ON CONFLICT(owner, kind, id) DO UPDATE SET value = excluded.value
            """, (owner, kind, item["id"], value))

    async def remove(self, owner: str, kind: str, item_id: str) -> None:
        with self.connect() as database:
            database.execute(
                "DELETE FROM items WHERE owner = ? AND kind = ? AND id = ?", (owner, kind, item_id)
            )


class ProxyInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    protocol: Literal["socks5", "http"]
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    username: SecretStr = Field(default=SecretStr(""), max_length=255)
    password: SecretStr = Field(default=SecretStr(""), max_length=255)


class StartInput(BaseModel):
    phone: str = Field(pattern=r"^\+[1-9][0-9]{6,14}$")
    proxy_id: str | None = Field(default=None, max_length=40)


class CodeInput(BaseModel):
    code: SecretStr = Field(min_length=4, max_length=10)


class PasswordInput(BaseModel):
    password: SecretStr = Field(min_length=1, max_length=256)


@dataclass
class Attempt:
    id: str
    login: LoginState
    proxy_id: str | None
    expires: float
    step: Literal["code", "password"] = "code"
    failures: int = 0


def proxy_summary(item: dict) -> dict:
    return {key: item[key] for key in ("id", "name", "protocol", "host", "port")}


def account_summary(item: dict) -> dict:
    return {key: item[key] for key in ("id", "first_name", "last_name", "username", "proxy_id")}


def create_app(settings: Settings | None = None, transport=None) -> FastAPI:
    pending: dict[str, Attempt] = {}
    cooldowns: dict[str, float] = {}
    mutation = asyncio.Lock()

    async def clear_expired():
        while True:
            await asyncio.sleep(20)
            async with mutation:
                now = time.monotonic()
                for owner, attempt in list(pending.items()):
                    if attempt.expires <= now:
                        pending.pop(owner)
                for owner, expires in list(cooldowns.items()):
                    if expires <= now:
                        cooldowns.pop(owner)

    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings.from_environment()
        app.state.token = config.token.encode()
        app.state.telegram = transport or (TelegramClient(config.api_id, config.api_hash) if config.api_id else None)
        async with AsyncExitStack() as stack:
            if config.state_origin:
                client = await stack.enter_async_context(httpx.AsyncClient(
                    base_url=config.state_origin,
                    timeout=8,
                    headers={
                        "OAI-Sites-Authorization": "Bearer " + config.state_access,
                        "Authorization": "Bearer " + config.state_token,
                    },
                ))
                app.state.store = RemoteStore(client, config.cipher)
            else:
                app.state.store = Store(config)
            cleanup = asyncio.create_task(clear_expired())
            try:
                yield
            finally:
                cleanup.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup
                pending.clear()
                cooldowns.clear()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def owner_identity(
        authorization: str = Header(default=""), x_panel_user: str = Header(default="")
    ) -> str:
        provided = authorization.removeprefix("Bearer ").encode()
        if not authorization.startswith("Bearer ") or not hmac.compare_digest(provided, app.state.token):
            raise HTTPException(401, "Требуется вход в закрытую панель.")
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,160}", x_panel_user):
            raise HTTPException(401, "Требуется вход в закрытую панель.")
        return x_panel_user

    def telegram_client():
        client = app.state.telegram
        if client is None:
            raise HTTPException(503, "Добавь ключи приложения Telegram в настройках сервера.")
        return client

    async def load_proxy(owner: str, proxy_id: str | None) -> Proxy | None:
        if proxy_id is None:
            return None
        item = await app.state.store.get(owner, "proxy", proxy_id)
        if item is None:
            raise HTTPException(404, "Прокси не найден.")
        return Proxy(item["protocol"], item["host"], item["port"], item["username_secret"], item["password"])

    @app.middleware("http")
    async def control_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(_request, _error):
        return JSONResponse(status_code=422, content={"detail": "Проверь формат полей."})

    @app.exception_handler(StoreUnavailable)
    async def store_error(_request, _error):
        return JSONResponse(status_code=503, content={"detail": "Хранилище временно недоступно."})

    @app.exception_handler(TelegramProblem)
    async def telegram_error(_request, error):
        messages = {
            "proxy_address": (422, "Укажи доступный публичный адрес прокси."),
            "phone": (422, "Telegram не принял этот номер."),
            "confirmation": (422, "Telegram не принял код или пароль 2FA."),
            "expired": (410, "Запрос на вход истёк. Запроси новый код."),
            "rate_limit": (429, "Telegram просит подождать перед следующей попыткой."),
            "revoked": (409, "Сессия завершена в Telegram. Подключи аккаунт снова."),
            "connection": (502, "Не удалось установить соединение с Telegram."),
            "telegram_rejected": (502, "Telegram отклонил операцию."),
        }
        status, message = messages[error.reason]
        headers = {"Retry-After": str(error.retry_after)} if error.retry_after else None
        return JSONResponse(status_code=status, content={"detail": message}, headers=headers)

    def ensure_wait_elapsed(owner: str) -> None:
        remaining = math.ceil(cooldowns.get(owner, 0) - time.monotonic())
        if remaining > 0:
            raise HTTPException(
                status_code=429, detail="Перед отправкой нового кода подожди.",
                headers={"Retry-After": str(remaining)},
            )

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/api/telegram")
    async def dashboard(owner: str = Depends(owner_identity)):
        accounts, proxies = await asyncio.gather(
            app.state.store.list(owner, "account"), app.state.store.list(owner, "proxy")
        )
        return {
            "telegram_ready": app.state.telegram is not None,
            "accounts": [account_summary(item) for item in accounts],
            "proxies": [proxy_summary(item) for item in proxies],
        }

    @app.post("/api/telegram/proxies", status_code=201)
    async def add_proxy(body: ProxyInput, owner: str = Depends(owner_identity)):
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "Укажи название прокси.")
        proxy = Proxy(
            body.protocol, normalize_proxy_host(body.host), body.port,
            body.username.get_secret_value(), body.password.get_secret_value(),
        )
        async with mutation:
            await telegram_client().test_proxy(proxy)
            item = {
                "id": secrets.token_urlsafe(18),
                "name": name,
                "protocol": proxy.protocol,
                "host": proxy.host,
                "port": proxy.port,
                "username_secret": proxy.username,
                "password": proxy.password,
            }
            await app.state.store.save(owner, "proxy", item)
        return proxy_summary(item)

    @app.delete("/api/telegram/proxies/{proxy_id}", status_code=204)
    async def remove_proxy(proxy_id: str, owner: str = Depends(owner_identity)):
        async with mutation:
            active = pending.get(owner)
            accounts = await app.state.store.list(owner, "account")
            used = active and active.expires > time.monotonic() and active.proxy_id == proxy_id
            if used or any(account["proxy_id"] == proxy_id for account in accounts):
                raise HTTPException(409, "Этот прокси используется аккаунтом или при входе.")
            await app.state.store.remove(owner, "proxy", proxy_id)

    @app.post("/api/telegram/login", status_code=201)
    async def start_login(body: StartInput, owner: str = Depends(owner_identity)):
        async with mutation:
            ensure_wait_elapsed(owner)
            proxy = await load_proxy(owner, body.proxy_id)
            try:
                login = await telegram_client().start_login(body.phone, proxy)
            except TelegramProblem as error:
                if error.retry_after:
                    cooldowns[owner] = time.monotonic() + error.retry_after
                raise
            now = time.monotonic()
            cooldowns[owner] = now + 30
            attempt = Attempt(secrets.token_urlsafe(24), login, body.proxy_id, now + 300)
            pending[owner] = attempt
        return {"id": attempt.id, "step": attempt.step}

    async def confirm(owner: str, login_id: str, value: str, step: Literal["code", "password"]):
        async with mutation:
            attempt = pending.get(owner)
            if not attempt or attempt.id != login_id or attempt.expires <= time.monotonic():
                raise TelegramProblem("expired")
            if step != attempt.step:
                raise HTTPException(409, "Продолжи вход с текущего шага.")
            if attempt.failures >= 5:
                pending.pop(owner)
                raise TelegramProblem("expired")
            proxy = await load_proxy(owner, attempt.proxy_id)
            client = telegram_client()
            try:
                result = await client.confirm(attempt.login, value, step, proxy)
            except TelegramProblem as error:
                if error.reason == "confirmation":
                    attempt.failures += 1
                elif error.reason in {"expired", "rate_limit", "revoked"}:
                    pending.pop(owner)
                    if error.retry_after:
                        cooldowns[owner] = time.monotonic() + error.retry_after
                raise
            if isinstance(result, LoginState):
                attempt.login = result
                attempt.step = "password"
                return {"id": attempt.id, "step": attempt.step}
            account_id = str(result.telegram_id)
            pending.pop(owner)
            item = {"id": account_id, "proxy_id": attempt.proxy_id, **asdict(result)}
            try:
                if await app.state.store.get(owner, "account", account_id):
                    await client.logout(result.session, proxy)
                    raise HTTPException(409, "Этот аккаунт уже подключён.")
                await app.state.store.save(owner, "account", item)
            except (sqlite3.Error, StoreUnavailable) as error:
                with suppress(TelegramProblem):
                    await client.logout(result.session, proxy)
                raise HTTPException(503, "Хранилище временно недоступно. Повтори вход позже.") from error
        return {"step": "done", "account": account_summary(item)}

    @app.post("/api/telegram/login/{login_id}/code")
    async def confirm_code(login_id: str, body: CodeInput, owner: str = Depends(owner_identity)):
        code = body.code.get_secret_value().strip()
        if not code.isascii() or not code.isdecimal():
            raise HTTPException(422, "Введи цифры из кода Telegram.")
        return await confirm(owner, login_id, code, "code")

    @app.post("/api/telegram/login/{login_id}/password")
    async def confirm_password(login_id: str, body: PasswordInput, owner: str = Depends(owner_identity)):
        return await confirm(owner, login_id, body.password.get_secret_value(), "password")

    @app.delete("/api/telegram/login/{login_id}", status_code=204)
    async def cancel(login_id: str, owner: str = Depends(owner_identity)):
        async with mutation:
            attempt = pending.get(owner)
            if attempt and attempt.id == login_id:
                pending.pop(owner)

    @app.delete("/api/telegram/accounts/{account_id}", status_code=204)
    async def disconnect(account_id: str, owner: str = Depends(owner_identity)):
        async with mutation:
            account = await app.state.store.get(owner, "account", account_id)
            if account is None:
                return
            proxy = await load_proxy(owner, account["proxy_id"])
            try:
                await telegram_client().logout(account["session"], proxy)
            except TelegramProblem as error:
                if error.reason != "revoked":
                    raise
            await app.state.store.remove(owner, "account", account_id)

    return app


app = create_app()
