from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal


class TelegramProblem(Exception):
    def __init__(self, reason: str, retry_after: int | None = None):
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason)


@dataclass(frozen=True)
class Proxy:
    protocol: Literal["socks5", "http"]
    host: str
    port: int
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)


@dataclass(frozen=True)
class LoginState:
    session: str = field(repr=False)
    code_hash: str = field(repr=False)
    phone: str = field(repr=False)


@dataclass(frozen=True)
class TelegramAccount:
    session: str = field(repr=False)
    telegram_id: int
    first_name: str
    last_name: str
    username: str


def normalize_proxy_host(host: str) -> str:
    host = host.strip().rstrip(".")
    try:
        return str(ipaddress.IPv4Address(host))
    except ipaddress.AddressValueError:
        pass
    try:
        name = host.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise TelegramProblem("proxy_address") from error
    label = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
    if len(name) > 253 or "." not in name or any(not label.fullmatch(part) for part in name.split(".")):
        raise TelegramProblem("proxy_address")
    return name


async def telegram_proxy(proxy: Proxy) -> dict:
    """Use a resolved public address as the proxy endpoint for this connection."""
    host = normalize_proxy_host(proxy.host)
    try:
        answers = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, proxy.port, family=socket.AF_INET, type=socket.SOCK_STREAM),
            timeout=4,
        )
    except (OSError, TimeoutError) as error:
        raise TelegramProblem("proxy_address") from error
    addresses = {ipaddress.IPv4Address(answer[4][0]) for answer in answers}
    if not addresses or any(not address.is_global or address.is_multicast or address.is_reserved for address in addresses):
        raise TelegramProblem("proxy_address")
    return {
        "proxy_type": proxy.protocol,
        "addr": str(min(addresses)),
        "port": proxy.port,
        "username": proxy.username or None,
        "password": proxy.password or None,
        "rdns": True,
    }


class TelegramClient:
    def __init__(self, api_id: int, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash

    @asynccontextmanager
    async def _connection(self, session: str, proxy: Proxy | None):
        from python_socks import ProxyError
        from telethon import TelegramClient as Client, errors
        from telethon.sessions import StringSession

        route = await telegram_proxy(proxy) if proxy else None
        client = Client(
            StringSession(session),
            self.api_id,
            self.api_hash,
            proxy=route,
            device_model="Панель сторис",
            app_version="1.0",
            receive_updates=False,
            auto_reconnect=False,
            connection_retries=0,
            request_retries=1,
            flood_sleep_threshold=0,
            timeout=8,
        )
        try:
            async with asyncio.timeout(25):
                await client.connect()
                yield client
        except errors.FloodWaitError as error:
            raise TelegramProblem("rate_limit", max(1, error.seconds)) from None
        except errors.PhoneNumberInvalidError:
            raise TelegramProblem("phone") from None
        except (errors.PhoneCodeInvalidError, errors.PasswordHashInvalidError):
            raise TelegramProblem("confirmation") from None
        except errors.PhoneCodeExpiredError:
            raise TelegramProblem("expired") from None
        except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError):
            raise TelegramProblem("revoked") from None
        except errors.RPCError:
            raise TelegramProblem("telegram_rejected") from None
        except (ProxyError, OSError, TimeoutError):
            raise TelegramProblem("connection") from None
        finally:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=3)
            except (OSError, TimeoutError):
                pass

    async def test_proxy(self, proxy: Proxy) -> None:
        async with self._connection("", proxy):
            pass

    async def start_login(self, phone: str, proxy: Proxy | None) -> LoginState:
        async with self._connection("", proxy) as client:
            sent = await client.send_code_request(phone)
            return LoginState(client.session.save(), sent.phone_code_hash, phone)

    async def confirm(
        self, pending: LoginState, value: str, step: Literal["code", "password"], proxy: Proxy | None
    ) -> TelegramAccount | LoginState:
        from telethon import errors

        async with self._connection(pending.session, proxy) as client:
            if step == "code":
                try:
                    user = await client.sign_in(
                        phone=pending.phone, code=value, phone_code_hash=pending.code_hash
                    )
                except errors.SessionPasswordNeededError:
                    return LoginState(client.session.save(), pending.code_hash, pending.phone)
            else:
                user = await client.sign_in(password=value)
            if not user or not getattr(user, "id", None) or getattr(user, "bot", False):
                raise TelegramProblem("telegram_rejected")
            return TelegramAccount(
                client.session.save(), user.id, user.first_name or "", user.last_name or "", user.username or ""
            )

    async def logout(self, session: str, proxy: Proxy | None) -> None:
        async with self._connection(session, proxy) as client:
            if not await client.log_out():
                raise TelegramProblem("telegram_rejected")
