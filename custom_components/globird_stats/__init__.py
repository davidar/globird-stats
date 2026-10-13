"""GloBird Energy: hourly grid import and cost statistics for the Energy panel, plus the account balance."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import GloBirdConfigEntry, GloBirdCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# 0.1.x was configured with a bare `globird_stats:` line in configuration.yaml. Accept it for one release.
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Warn about the legacy YAML key; setup happens through config entries."""
    if DOMAIN in config:
        _LOGGER.warning(
            "configure GloBird Energy through the UI and remove the globird_stats: line from configuration.yaml"
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GloBirdConfigEntry) -> bool:
    """Log in, fetch, sync statistics, set up the sensors."""
    coordinator = GloBirdCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await coordinator.client.async_close()
        raise
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GloBirdConfigEntry) -> bool:
    """Unload the sensors; the coordinator's shutdown (registered on the entry) closes the portal session."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
