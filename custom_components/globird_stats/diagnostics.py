"""Diagnostics: config entry and the latest coordinator data, with personal fields redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_EMAIL, CONF_PASSWORD
from .coordinator import GloBirdConfigEntry

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    "emailAddress",
    "title",  # the entry title is the account number
    "accountNumber",
    "account_number",
    "accountName",
    "accountAddress",
    "address",
    "siteAddress",
    "streetAddress",
    "postCode",
    "post_code",
    "identifier",
    "siteIdentifier",
    "nmi",
    "serial",
    "serialNumber",
    "serial_number",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: GloBirdConfigEntry) -> dict[str, Any]:
    """Return redacted diagnostics for one account."""
    coordinator = entry.runtime_data
    data = asdict(coordinator.data) if coordinator.data is not None else None
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "data": async_redact_data(data, TO_REDACT) if data is not None else None,
    }
