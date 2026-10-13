"""Tests for the GloBird portal client (all HTTP mocked; no real portal)."""

from __future__ import annotations

import asyncio
import base64
from datetime import date
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aioresponses import CallbackResult, aioresponses
from aioresponses import core as aioresponses_core
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from yarl import URL

from custom_components.globird_stats.api import (
    USER_AGENT,
    GloBirdAuthError,
    GloBirdCaptchaRequired,
    GloBirdClient,
    GloBirdError,
    GloBirdSessionExpired,
    PowerService,
    power_services,
)
from custom_components.globird_stats.const import BASE_URL


class _CompatResponse(aiohttp.ClientResponse):
    """aiohttp 3.14 requires `stream_writer`; aioresponses 0.7.9 doesn't pass one."""

    def __init__(self, *args: Any, stream_writer: Any = None, **kwargs: Any) -> None:
        if stream_writer is None:
            stream_writer = SimpleNamespace(output_size=0)
        super().__init__(*args, stream_writer=stream_writer, **kwargs)


@pytest.fixture(autouse=True)
def _aioresponses_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aioresponses_core, "ClientResponse", _CompatResponse)


EMAIL = "someone@example.com"
PASSWORD = "correct horse battery staple"
BACKEND = URL("https://portal-backend.azurewebsites.net/")

HOME = BASE_URL
JWK_URL = f"{BASE_URL}/api/account/publicjwk"
LOGIN_URL = f"{BASE_URL}/api/account/login"
USER_URL = f"{BASE_URL}/api/account/currentuser"
BALANCE_URL = f"{BASE_URL}/api/transaction/balance?accountId=1001"

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64url(number: int) -> str:
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


_PUB = PRIVATE_KEY.public_key().public_numbers()
JWK = {"kty": "RSA", "alg": "RSA-OAEP-256", "e": _b64url(_PUB.e), "n": _b64url(_PUB.n)}

CURRENT_USER: dict[str, Any] = {
    "accounts": [
        {
            "accountId": 1001,
            "accountNumber": "G0001001",
            "services": [
                {
                    "accountServiceId": 5001,
                    "serviceType": "Power",
                    "status": "Switched",
                    "siteIdentifier": "6407123456",
                    "postCode": "3000",
                    "state": "VIC",
                },
                {"accountServiceId": 5002, "serviceType": "Gas", "status": "Switched"},
                {
                    "accountServiceId": 5003,
                    "serviceType": "Power",
                    "status": "Switched",
                    "closedDate": "2025-01-31",
                },
                {
                    "accountServiceId": 5004,
                    "serviceType": "Electricity",
                    "status": "Closed",
                },
            ],
        },
        {
            "accountId": 1002,
            "accountNumber": None,
            "services": [
                {
                    "accountServiceId": 6001,
                    "serviceType": "ELECTRICITY",
                    "nmi": "6407654321",
                }
            ],
        },
    ]
}


def envelope(data: Any, *, success: bool = True, message: str = "") -> dict[str, Any]:
    return {"data": data, "message": message, "success": success}


LOGIN_OK = envelope({"isLoginSucceeded": True})


def mock_login(
    m: aioresponses,
    *,
    login: dict[str, Any] | None = None,
    home: Any = None,
    on_login: Any = None,
) -> None:
    """Register one full login sequence."""
    if isinstance(home, Exception):
        m.get(HOME, exception=home)
    else:
        m.get(
            HOME,
            status=200,
            body="<html></html>",
            content_type="text/html",
            callback=home,
        )
    m.get(JWK_URL, payload=JWK)
    m.post(LOGIN_URL, payload=login or LOGIN_OK, callback=on_login)
    if (login or LOGIN_OK) is LOGIN_OK:
        m.get(USER_URL, payload=envelope(CURRENT_USER))


def calls(m: aioresponses, method: str, url: str) -> list[Any]:
    return m.requests.get((method, URL(url)), [])


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as sess:
        yield sess


@pytest_asyncio.fixture
async def client(session: aiohttp.ClientSession):
    yield GloBirdClient(EMAIL, PASSWORD, session=session)


# -- login -----------------------------------------------------------------


async def test_login_happy_path(client: GloBirdClient) -> None:
    with aioresponses() as m:
        mock_login(m)
        user = await client.async_login()

    assert user == CURRENT_USER
    (login_call,) = calls(m, "POST", LOGIN_URL)
    body = login_call.kwargs["json"]
    assert body["emailAddress"] == EMAIL
    assert body["rememberMe"] is False
    sha256 = hashes.SHA256()
    plain = PRIVATE_KEY.decrypt(
        base64.b64decode(body["password"]),
        padding.OAEP(mgf=padding.MGF1(algorithm=sha256), algorithm=sha256, label=None),
    )
    assert plain.decode() == PASSWORD
    headers = login_call.kwargs["headers"]
    assert headers["User-Agent"] == USER_AGENT
    assert USER_AGENT.startswith("globird_stats/") and USER_AGENT.endswith("(Home Assistant)")
    assert headers["Origin"] == BASE_URL
    assert headers["Referer"] == f"{BASE_URL}/"


@pytest.mark.parametrize("flag", ["requireRetryCaptCha", "requireHCaptcha"])
async def test_login_captcha(client: GloBirdClient, flag: str) -> None:
    with aioresponses() as m:
        mock_login(m, login=envelope({flag: True, "isLoginSucceeded": False}, success=False))
        with pytest.raises(GloBirdCaptchaRequired) as err:
            await client.async_login()
    assert isinstance(err.value, GloBirdAuthError)


@pytest.mark.parametrize(
    "result",
    [
        envelope({"isLoginSucceeded": False}, success=False, message="Invalid credentials"),
        envelope({"isLoginSucceeded": False}, success=True),
        envelope(None, success=False),
    ],
)
async def test_login_wrong_password(client: GloBirdClient, result: dict[str, Any]) -> None:
    with aioresponses() as m:
        mock_login(m, login=result)
        with pytest.raises(GloBirdAuthError) as err:
            await client.async_login()
    assert not isinstance(err.value, GloBirdCaptchaRequired)
    assert not calls(m, "GET", USER_URL)


async def test_sticky_cookie_copied_to_portal_host(client: GloBirdClient, session: aiohttp.ClientSession) -> None:
    def set_backend_cookies(url: URL, **kwargs: Any) -> None:
        # What Azure does: routing cookies scoped to the backend host.
        session.cookie_jar.update_cookies(
            {
                "ARRAffinity": "shard-7",
                "ARRAffinitySameSite": "shard-7",
                "unrelated": "x",
            },
            BACKEND,
        )

    with aioresponses() as m:
        mock_login(m, home=set_backend_cookies)
        await client.async_login()

    portal = session.cookie_jar.filter_cookies(URL(f"{BASE_URL}/api/account/currentuser"))
    assert portal["ARRAffinity"].value == "shard-7"
    assert portal["ARRAffinitySameSite"].value == "shard-7"
    assert "unrelated" not in portal


async def test_home_page_failure_is_not_fatal(client: GloBirdClient) -> None:
    with aioresponses() as m:
        mock_login(m, home=aiohttp.ClientConnectionError("boom"))
        assert await client.async_login() == CURRENT_USER


async def test_login_tolerates_wrapped_jwk(client: GloBirdClient) -> None:
    with aioresponses() as m:
        m.get(HOME, status=200, body="")
        m.get(JWK_URL, payload=envelope(JWK))
        m.post(LOGIN_URL, payload=LOGIN_OK)
        m.get(USER_URL, payload=envelope(CURRENT_USER))
        assert await client.async_login() == CURRENT_USER


# -- session expiry --------------------------------------------------------


async def test_401_relogin_then_retry_succeeds(client: GloBirdClient) -> None:
    with aioresponses() as m:
        m.get(BALANCE_URL, status=401)
        mock_login(m)
        m.get(BALANCE_URL, payload=envelope({"balance": -12.5, "maxRefundableAmount": 0}))
        result = await client.async_balance(1001)

    assert result == {"balance": -12.5, "maxRefundableAmount": 0}
    assert len(calls(m, "POST", LOGIN_URL)) == 1
    assert len(calls(m, "GET", BALANCE_URL)) == 2


async def test_401_twice_raises_session_expired(client: GloBirdClient) -> None:
    with aioresponses() as m:
        m.get(BALANCE_URL, status=403)
        mock_login(m)
        m.get(BALANCE_URL, status=401)
        with pytest.raises(GloBirdSessionExpired):
            await client.async_balance(1001)

    assert len(calls(m, "POST", LOGIN_URL)) == 1
    assert len(calls(m, "GET", BALANCE_URL)) == 2


async def test_401_relogin_rejected_raises_auth_error(client: GloBirdClient) -> None:
    with aioresponses() as m:
        m.get(BALANCE_URL, status=401)
        mock_login(m, login=envelope({"isLoginSucceeded": False}, success=False))
        with pytest.raises(GloBirdAuthError):
            await client.async_balance(1001)
    assert len(calls(m, "GET", BALANCE_URL)) == 1


async def test_concurrent_401s_share_one_relogin(client: GloBirdClient) -> None:
    state = {"logged_in": False}

    def on_login(url: URL, **kwargs: Any) -> None:
        state["logged_in"] = True

    def balance(url: URL, **kwargs: Any) -> CallbackResult:
        if not state["logged_in"]:
            return CallbackResult(status=401)
        return CallbackResult(payload=envelope({"balance": 1.0}))

    with aioresponses() as m:
        m.get(BALANCE_URL, callback=balance, repeat=True)
        mock_login(m, on_login=on_login)
        results = await asyncio.gather(client.async_balance(1001), client.async_balance(1001))

    assert results == [{"balance": 1.0}, {"balance": 1.0}]
    assert len(calls(m, "POST", LOGIN_URL)) == 1


# -- envelope and transport errors -----------------------------------------


async def test_success_false_raises_error(client: GloBirdClient) -> None:
    with aioresponses() as m:
        m.get(
            BALANCE_URL,
            payload=envelope(None, success=False, message="Unable to get balance."),
        )
        with pytest.raises(GloBirdError, match="Unable to get balance") as err:
            await client.async_balance(1001)
    assert not isinstance(err.value, (GloBirdAuthError, GloBirdSessionExpired))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": 500},
        {"status": 200, "body": "<html>not json</html>"},
        {"exception": aiohttp.ClientConnectionError("down")},
        {"exception": TimeoutError()},
    ],
)
async def test_transport_failures_raise_error(client: GloBirdClient, kwargs: dict) -> None:
    with aioresponses() as m:
        m.get(BALANCE_URL, **kwargs)
        with pytest.raises(GloBirdError):
            await client.async_balance(1001)


# -- data calls ------------------------------------------------------------


async def test_current_user(client: GloBirdClient) -> None:
    with aioresponses() as m:
        m.get(USER_URL, payload=envelope(CURRENT_USER))
        assert await client.async_current_user() == CURRENT_USER


async def test_balance_query(client: GloBirdClient) -> None:
    with aioresponses() as m:
        m.get(
            BALANCE_URL,
            payload=envelope({"balance": -75.0, "maxRefundableAmount": 70.0}),
        )
        assert await client.async_balance("1001") == {
            "balance": -75.0,
            "maxRefundableAmount": 70.0,
        }


METER = {
    "serialNumber": "ABC123",
    "meterReadType": "Smart",
    "meterStatus": "Active",
    "siteIdentifier": "6407123456",
}


@pytest.mark.parametrize(
    "data",
    [
        [METER],
        {"data": [METER]},
        {"meters": [METER]},
        {"items": [METER]},
        {"readMeters": [METER]},
    ],
)
async def test_read_meters_shapes(client: GloBirdClient, data: Any) -> None:
    with aioresponses() as m:
        m.get(
            f"{BASE_URL}/api/site/readmeters?accountServiceId=5001",
            payload=envelope(data),
        )
        assert await client.async_read_meters(5001) == [METER]


@pytest.mark.parametrize("data", [None, {}, {"other": 1}])
async def test_read_meters_empty(client: GloBirdClient, data: Any) -> None:
    with aioresponses() as m:
        m.get(
            f"{BASE_URL}/api/site/readmeters?accountServiceId=5001",
            payload=envelope(data),
        )
        assert await client.async_read_meters(5001) == []


async def test_smart_meter_reads(client: GloBirdClient) -> None:
    path = "/api/site/accountservicetimezonesmartmeterread"
    url = f"{BASE_URL}{path}?accountServiceId=5001"
    rows = [
        {
            "readDate": "2026/09/21",
            "suffix": "E1",
            "usageArray": [0.25] * 48,
            "usage": 12.0,
            "chargeType": "Peak Usage",
            "chargeCategoryCode": "USAGE",
            "meterStatus": "Energized",
            "serial": "ABC123",
            "minQualityMethod": "A",
        }
    ]
    with aioresponses() as m:
        m.post(url, payload=envelope(rows))
        result = await client.async_smart_meter_reads(
            5001, "6407123456", "ABC123", date(2026, 9, 21), date(2026, 10, 12)
        )
    assert result == rows
    (call,) = calls(m, "POST", url)
    assert call.kwargs["json"] == {
        "identifier": "6407123456",
        "serialNumber": "ABC123",
        "fromDate": "2026/09/21",
        "toDate": "2026/10/12",
        "isSmart": True,
        "isAcrossAccount": False,
    }


async def test_cost_detail(client: GloBirdClient) -> None:
    url = f"{BASE_URL}/api/transaction/CostDetail"
    rows = [
        {
            "chargeCategory": "USAGE",
            "chargeType": None,
            "date": "2026/09/21",
            "amount": 3.1,
            "quantity": 12.0,
            "isMeterReadMissingOnDate": False,
        },
        {
            "chargeCategory": "SUPPLY",
            "chargeType": None,
            "date": "2026/09/21",
            "amount": 1.0,
            "quantity": 1.0,
            "isMeterReadMissingOnDate": False,
        },
    ]
    with aioresponses() as m:
        m.post(url, payload=envelope(rows))
        result = await client.async_cost_detail(5001, "6407123456", date(2026, 9, 1), date(2026, 10, 12))
    assert result == rows
    (call,) = calls(m, "POST", url)
    assert call.kwargs["json"] == {
        "accountServiceId": 5001,
        "identifier": "6407123456",
        "from": "2026-09-01",
        "to": "2026-10-12",
        "isSmart": True,
    }


async def test_list_endpoints_tolerate_null_data(client: GloBirdClient) -> None:
    with aioresponses() as m:
        m.post(f"{BASE_URL}/api/transaction/CostDetail", payload=envelope(None))
        assert await client.async_cost_detail(1, "x", date(2026, 1, 1), date(2026, 1, 2)) == []


# -- sessions --------------------------------------------------------------


async def test_owned_session_closed() -> None:
    client = GloBirdClient(EMAIL, PASSWORD)
    owned = client._session
    assert isinstance(owned.cookie_jar, aiohttp.CookieJar)
    await client.async_close()
    assert owned.closed
    await client.async_close()  # idempotent


async def test_passed_session_not_closed(client: GloBirdClient, session: aiohttp.ClientSession) -> None:
    await client.async_close()
    assert not session.closed


# -- power_services --------------------------------------------------------


def test_power_services_filters_and_orders() -> None:
    assert power_services(CURRENT_USER) == [
        PowerService(
            account_id="1001",
            account_number="G0001001",
            account_service_id="5001",
            identifier="6407123456",
            post_code="3000",
            state="VIC",
            status="Switched",
        ),
        PowerService(
            account_id="1002",
            account_number=None,
            account_service_id="6001",
            identifier="6407654321",
            post_code=None,
            state=None,
            status=None,
        ),
    ]


def test_power_services_empty() -> None:
    assert power_services({}) == []
    assert power_services({"accounts": None}) == []
    assert power_services({"accounts": [{"accountId": 1, "services": None}]}) == []
