"""Config flow: email and password, with reauth when the portal stops accepting the login."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .api import GloBirdAuthError, GloBirdCaptchaRequired, GloBirdClient, GloBirdError
from .const import CONF_EMAIL, CONF_PASSWORD, DOMAIN

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema({vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str})
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


async def _try_login(email: str, password: str) -> tuple[dict[str, Any] | None, str | None]:
    """Log in once. Returns (first account, None) or (None, error key)."""
    client = GloBirdClient(email, password)
    try:
        user = await client.async_login()
    except GloBirdCaptchaRequired:
        return None, "captcha_required"
    except GloBirdAuthError:
        return None, "invalid_auth"
    except (GloBirdError, aiohttp.ClientError, TimeoutError):
        return None, "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error logging in to GloBird")
        return None, "unknown"
    finally:
        await client.async_close()
    accounts = [a for a in (user.get("accounts") or []) if isinstance(a, dict) and a.get("accountId") is not None]
    if not accounts:
        return None, "no_accounts"
    return accounts[0], None


class GloBirdConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a GloBird Energy config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the portal login."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            account, error = await _try_login(email, user_input[CONF_PASSWORD])
            if error:
                errors["base"] = error
            else:
                assert account is not None
                await self.async_set_unique_id(str(account["accountId"]))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=str(account.get("accountNumber") or email),
                    data={CONF_EMAIL: email, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The stored login stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the password again."""
        entry = self._get_reauth_entry()
        email = entry.data[CONF_EMAIL]
        errors: dict[str, str] = {}
        if user_input is not None:
            account, error = await _try_login(email, user_input[CONF_PASSWORD])
            if error:
                errors["base"] = error
            else:
                assert account is not None
                await self.async_set_unique_id(str(account["accountId"]))
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={"email": email},
            errors=errors,
        )
