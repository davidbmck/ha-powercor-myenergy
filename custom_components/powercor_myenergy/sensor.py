"""A diagnostic timestamp, without customer metadata or readings."""

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([LastUpdateSensor(entry), LatestSourceSensor(entry)])


class LastUpdateSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "myEnergy last successful update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry):
        super().__init__(entry.runtime_data)
        self._attr_unique_id = f"{entry.entry_id}_last_update"

    @property
    def native_value(self):
        return self.coordinator.data["last_update"]

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        return {key: data.get(key) for key in (
            "last_successful_sync", "sync_complete", "skipped_meters", "incomplete_hours",
            "unresolved_history_start",
        )}


class LatestSourceSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "myEnergy latest source timestamp"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry):
        super().__init__(entry.runtime_data)
        self._attr_unique_id = f"{entry.entry_id}_latest_source"

    @property
    def native_value(self):
        value = self.coordinator.data.get("latest_source_timestamp")
        return datetime.fromisoformat(value) if value else None
