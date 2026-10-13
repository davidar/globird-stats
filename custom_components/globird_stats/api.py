"""Client for the GloBird Energy customer portal.

The portal is a browser app backed by a JSON API. Every response is an envelope
``{"data": ..., "message": str, "success": bool}``; the session lives in cookies.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from yarl import URL

from .const import BASE_URL

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)
_UNAUTHORIZED = (401, 403)
_STICKY_COOKIE_MARKER = "arraffinity"
_METER_LIST_KEYS = ("data", "meters", "items", "readMeters")


def _integration_version() -> str:
    """Version from manifest.json, or a fallback when it is missing or unreadable."""
    try:
        manifest = json.loads((Path(__file__).parent / "manifest.json").read_text("utf-8"))
        return str(manifest["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return "0.2.0"


# Read once at import: HA imports integrations in an executor, so the file read
# never runs inside the event loop.
USER_AGENT = f"globird_stats/{_integration_version()} (Home Assistant)"


class GloBirdError(Exception):
    """The portal could not be reached or answered with an error."""


class GloBirdAuthError(GloBirdError):
    """The portal rejected the login."""


class GloBirdCaptchaRequired(GloBirdAuthError):
    """The portal wants a captcha before it will accept another login."""


class GloBirdSessionExpired(GloBirdError):
    """The portal kept refusing the session even after a fresh login."""


class _Unauthorized(GloBirdError):
    """Internal: HTTP 401/403 on a request."""


@dataclass(frozen=True)
class PowerService:
    """An open electricity service on a GloBird account."""

    account_id: str
    account_number: str | None
    account_service_id: str
    identifier: str | None  # the NMI (siteIdentifier)
    post_code: str | None
    state: str | None
    status: str | None


def _str_or_none(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def power_services(current_user: dict[str, Any]) -> list[PowerService]:
    """Open electricity services in a currentuser `data` dict, in portal order."""
    found: list[PowerService] = []
    for account in current_user.get("accounts") or []:
        for service in account.get("services") or []:
            kind = str(service.get("serviceType") or "").lower()
            if "power" not in kind and "electric" not in kind:
                continue
            if service.get("closedDate"):
                continue
            if str(service.get("status") or "").lower() == "closed":
                continue
            found.append(
                PowerService(
                    account_id=str(account.get("accountId")),
                    account_number=_str_or_none(account.get("accountNumber")),
                    account_service_id=str(service.get("accountServiceId")),
                    identifier=_str_or_none(service.get("siteIdentifier") or service.get("nmi")),
                    post_code=_str_or_none(service.get("postCode")),
                    state=_str_or_none(service.get("state")),
                    status=_str_or_none(service.get("status")),
                )
            )
    return found


def _b64url_int(value: str) -> int:
    return int.from_bytes(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big")


def encrypt_password(jwk: dict[str, Any], password: str) -> str:
    """RSA-OAEP (SHA-256, MGF1-SHA-256) under the portal's JWK, base64 encoded."""
    try:
        key = RSAPublicNumbers(_b64url_int(jwk["e"]), _b64url_int(jwk["n"])).public_key()
    except (KeyError, TypeError, ValueError) as err:
        raise GloBirdError("Portal returned an unusable public key") from err
    sha256 = hashes.SHA256()
    ciphertext = key.encrypt(
        password.encode(),
        padding.OAEP(mgf=padding.MGF1(algorithm=sha256), algorithm=sha256, label=None),
    )
    return base64.b64encode(ciphertext).decode("ascii")


class GloBirdClient:
    """Async client for one GloBird portal login."""

    def __init__(
        self,
        email: str,
        password: str,
        *,
        session: aiohttp.ClientSession | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        """Set up the client; nothing is sent until the first call."""
        self._email = email
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._owns_session = session is None
        self._session = session or aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self._headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self._base_url,
            "Referer": f"{self._base_url}/",
            "User-Agent": USER_AGENT,
        }
        self._login_lock = asyncio.Lock()
        self._login_count = 0

    async def async_close(self) -> None:
        """Close the HTTP session if this client created it."""
        if self._owns_session and not self._session.closed:
            await self._session.close()

    # -- transport ---------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        check_success: bool = True,
    ) -> dict[str, Any]:
        """One request, no retry. Returns the whole envelope."""
        try:
            async with self._session.request(
                method,
                f"{self._base_url}{path}",
                params=params,
                json=body,
                headers=self._headers,
                timeout=_TIMEOUT,
            ) as resp:
                status = resp.status
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GloBirdError(f"Cannot reach the GloBird portal ({path}): {err!r}") from err

        if status in _UNAUTHORIZED:
            raise _Unauthorized(f"GloBird portal refused {path} (HTTP {status})")
        if not 200 <= status < 300:
            raise GloBirdError(f"GloBird portal answered HTTP {status} for {path}")
        try:
            payload = json.loads(text)
        except ValueError as err:
            raise GloBirdError(f"GloBird portal sent invalid JSON for {path}") from err
        if not isinstance(payload, dict):
            raise GloBirdError(f"GloBird portal sent an unexpected body for {path}")
        if check_success and payload.get("success") is False:
            raise GloBirdError(payload.get("message") or f"GloBird portal request failed: {path}")
        return payload

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        """Authenticated request returning `data`; one re-login on 401/403, no loop."""
        seen_login = self._login_count
        try:
            payload = await self._request(method, path, params=params, body=body)
        except _Unauthorized:
            _LOGGER.debug("GloBird session refused on %s; logging in again", path)
            await self._relogin(seen_login)
            try:
                payload = await self._request(method, path, params=params, body=body)
            except _Unauthorized as err:
                raise GloBirdSessionExpired(f"GloBird portal still refuses {path} after a fresh login") from err
        return payload.get("data")

    async def _relogin(self, seen_login: int) -> None:
        """Log in again unless a concurrent call already did since `seen_login`."""
        async with self._login_lock:
            if self._login_count != seen_login:
                return
            self._session.cookie_jar.clear_domain(URL(self._base_url).host or "")
            await self._login()

    # -- login -------------------------------------------------------------

    async def _pin_backend(self) -> None:
        """Load the home page so Azure assigns a backend, and keep that assignment.

        Azure App Service sets its ARRAffinity routing cookies for the backend's
        *.azurewebsites.net domain, which the client never sends to the portal
        host; without them requests scatter across backends and the session is
        lost. Copy them onto the portal host. Best effort: the login reports any
        real failure.
        """
        try:
            async with self._session.get(self._base_url, headers=self._headers, timeout=_TIMEOUT) as resp:
                await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("GloBird home page fetch failed, logging in anyway: %r", err)
            return
        sticky = {
            cookie.key: cookie.value
            for cookie in self._session.cookie_jar
            if _STICKY_COOKIE_MARKER in cookie.key.lower()
        }
        if sticky:
            self._session.cookie_jar.update_cookies(sticky, URL(f"{self._base_url}/"))

    async def _login(self) -> dict[str, Any]:
        """Run the whole login sequence and return the current-user `data`."""
        await self._pin_backend()
        try:
            jwk = await self._request("GET", "/api/account/publicjwk", check_success=False)
        except _Unauthorized as err:
            raise GloBirdError("GloBird portal refused the public key request") from err
        if isinstance(jwk.get("data"), dict) and "n" in jwk["data"]:
            jwk = jwk["data"]  # tolerate the envelope, though the portal sends a bare JWK

        try:
            result = await self._request(
                "POST",
                "/api/account/login",
                body={
                    "emailAddress": self._email,
                    "password": encrypt_password(jwk, self._password),
                    "rememberMe": False,
                },
                check_success=False,
            )
        except _Unauthorized as err:
            raise GloBirdAuthError("GloBird portal rejected the login") from err

        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        if data.get("requireRetryCaptCha") or data.get("requireHCaptcha"):
            raise GloBirdCaptchaRequired("GloBird portal wants a captcha before the next login")
        if result.get("success") is not True or data.get("isLoginSucceeded") is False:
            reason = result.get("message") or data.get("message")
            raise GloBirdAuthError(f"GloBird login failed: {reason}" if reason else "GloBird login failed")
        self._login_count += 1

        try:
            user = await self._request("GET", "/api/account/currentuser")
        except _Unauthorized as err:
            raise GloBirdSessionExpired("GloBird portal dropped the session right after login") from err
        return user.get("data") or {}

    async def async_login(self) -> dict[str, Any]:
        """Log in and return the current-user `data` (accounts and services)."""
        async with self._login_lock:
            return await self._login()

    # -- data --------------------------------------------------------------

    async def async_current_user(self) -> dict[str, Any]:
        """`data` of /api/account/currentuser."""
        return await self._call("GET", "/api/account/currentuser") or {}

    async def async_balance(self, account_id: int | str) -> dict[str, Any]:
        """Balance `data`: balance (negative = in credit) and maxRefundableAmount."""
        return await self._call("GET", "/api/transaction/balance", params={"accountId": str(account_id)}) or {}

    async def async_read_meters(self, account_service_id: int | str) -> list[dict[str, Any]]:
        """Meters on a service (serialNumber, meterReadType, meterStatus, ...)."""
        data = await self._call(
            "GET",
            "/api/site/readmeters",
            params={"accountServiceId": str(account_service_id)},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in _METER_LIST_KEYS:
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    async def async_smart_meter_reads(
        self,
        account_service_id: int | str,
        identifier: str,
        serial_number: str,
        from_date: date,
        to_date: date,
    ) -> list[dict[str, Any]]:
        """Half-hourly smart-meter rows, one per day per register per charge type."""
        data = await self._call(
            "POST",
            "/api/site/accountservicetimezonesmartmeterread",
            params={"accountServiceId": str(account_service_id)},
            body={
                "identifier": identifier,
                "serialNumber": serial_number,
                "fromDate": from_date.strftime("%Y/%m/%d"),
                "toDate": to_date.strftime("%Y/%m/%d"),
                "isSmart": True,
                "isAcrossAccount": False,
            },
        )
        return data if isinstance(data, list) else []

    async def async_cost_detail(
        self,
        account_service_id: int | str,
        identifier: str,
        from_date: date,
        to_date: date,
    ) -> list[dict[str, Any]]:
        """Daily cost rows (USAGE and SUPPLY) for a service."""
        data = await self._call(
            "POST",
            "/api/transaction/CostDetail",
            body={
                "accountServiceId": account_service_id,
                "identifier": identifier,
                "from": from_date.isoformat(),
                "to": to_date.isoformat(),
                "isSmart": True,
            },
        )
        return data if isinstance(data, list) else []
