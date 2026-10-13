"""Config flow: user step, errors, duplicate account, reauth."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.globird_stats.api import GloBirdAuthError, GloBirdCaptchaRequired, GloBirdError
from custom_components.globird_stats.const import CONF_EMAIL, CONF_PASSWORD, DOMAIN

from .conftest import ACCOUNT_ID, ACCOUNT_NUMBER, EMAIL, ENTRY_DATA, PASSWORD

# Creating an entry sets up the integration's dependencies, recorder among them.
pytestmark = pytest.mark.usefixtures("recorder_mock")


@pytest.fixture(autouse=True)
def no_setup() -> AsyncMock:
    """Don't set the entry up after the flow creates it."""
    with patch("custom_components.globird_stats.async_setup_entry", return_value=True) as mock:
        yield mock


async def _start(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_user_creates_entry(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """A good login creates an entry keyed on the portal account id."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: f" {EMAIL} ", CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ACCOUNT_NUMBER
    assert result["data"] == ENTRY_DATA
    assert result["result"].unique_id == str(ACCOUNT_ID)
    mock_client.flow_cls.assert_called_once_with(EMAIL, PASSWORD)
    mock_client.async_close.assert_awaited()


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (GloBirdAuthError("nope"), "invalid_auth"),
        (GloBirdCaptchaRequired("captcha"), "captcha_required"),
        (GloBirdError("500"), "cannot_connect"),
        (TimeoutError(), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_errors_then_recover(
    hass: HomeAssistant, mock_client: MagicMock, exc: Exception, error: str
) -> None:
    """Login failures show the right error and the form can be retried."""
    good = mock_client.async_login.side_effect
    mock_client.async_login.side_effect = exc
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    mock_client.async_close.assert_awaited()

    mock_client.async_login.side_effect = good
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_no_accounts(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """A login with no accounts can't be added."""
    mock_client.async_login.side_effect = None
    mock_client.async_login.return_value = {"accounts": []}
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["errors"] == {"base": "no_accounts"}


async def test_user_already_configured(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """The same portal account can't be added twice."""
    MockConfigEntry(domain=DOMAIN, unique_id=str(ACCOUNT_ID), data=ENTRY_DATA).add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """Reauth asks for the password only, stores it and reloads."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=str(ACCOUNT_ID), title=ACCOUNT_NUMBER, data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "old"}
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["email"] == EMAIL

    mock_client.async_login.side_effect = GloBirdCaptchaRequired("captcha")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "captcha_required"}

    mock_client.async_login.side_effect = None
    mock_client.async_login.return_value = {"accounts": [{"accountId": ACCOUNT_ID}]}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {CONF_EMAIL: EMAIL, CONF_PASSWORD: "new"}
    mock_client.flow_cls.assert_called_with(EMAIL, "new")


async def test_reauth_wrong_account(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """A reauth login that lands on another account is refused."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=str(ACCOUNT_ID), data=ENTRY_DATA)
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    mock_client.async_login.side_effect = None
    mock_client.async_login.return_value = {"accounts": [{"accountId": 999}]}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "x"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data == ENTRY_DATA
