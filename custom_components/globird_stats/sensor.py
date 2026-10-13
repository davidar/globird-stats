"""Sensors: account balance and the newest day with complete interval data."""

from __future__ import annotations

from datetime import date

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BASE_URL, DOMAIN
from .coordinator import GloBirdConfigEntry, GloBirdCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: GloBirdConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the two sensors for this account."""
    coordinator = entry.runtime_data
    async_add_entities([GloBirdBalanceSensor(coordinator), GloBirdLatestDataDaySensor(coordinator)])


class GloBirdEntity(CoordinatorEntity[GloBirdCoordinator]):
    """Common device info: one device per account."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GloBirdCoordinator, key: str) -> None:
        """Set ids from the account id."""
        super().__init__(coordinator)
        account_id = coordinator.config_entry.unique_id or coordinator.data.account_id
        self._attr_unique_id = f"{account_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account_id)},
            name="GloBird",
            manufacturer="GloBird Energy",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=BASE_URL,
        )


class GloBirdBalanceSensor(GloBirdEntity, SensorEntity):
    """Account balance in the portal's sign: amount owing, so -75.16 means $75.16 in credit."""

    _attr_device_class = SensorDeviceClass.MONETARY
    # MONETARY only accepts TOTAL (HA warns on MEASUREMENT); a balance is a total that moves both ways.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "AUD"
    _attr_icon = "mdi:cash"

    def __init__(self, coordinator: GloBirdCoordinator) -> None:
        """Create the balance sensor."""
        super().__init__(coordinator, "balance")

    @property
    def native_value(self) -> float | None:
        """Balance as the portal reports it (positive = owing, negative = in credit)."""
        return self.coordinator.data.balance

    @property
    def extra_state_attributes(self) -> dict[str, float | None]:
        """Refundable amount."""
        return {"max_refundable": self.coordinator.data.max_refundable}


class GloBirdLatestDataDaySensor(GloBirdEntity, SensorEntity):
    """Newest day with a full 48 half-hours of import data, over all services."""

    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:calendar-check"

    def __init__(self, coordinator: GloBirdCoordinator) -> None:
        """Create the latest-data-day sensor."""
        super().__init__(coordinator, "latest_data_day")

    @property
    def native_value(self) -> date | None:
        """The day, in NEM time (AEST)."""
        return self.coordinator.data.latest_data_day
