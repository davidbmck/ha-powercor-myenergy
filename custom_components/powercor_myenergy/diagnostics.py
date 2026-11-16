"""Allow only operational status into downloaded diagnostics."""

from .const import CONF_AUTOMATIC


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = getattr(entry, "runtime_data", None)
    data = coordinator.data if coordinator and coordinator.data else {}
    updated = data.get("last_update")
    return {
        "automatic_login": bool(entry.data.get(CONF_AUTOMATIC)),
        "last_update_success": coordinator.last_update_success if coordinator else False,
        "last_update": updated.isoformat() if updated else None,
        "meter_count": data.get("meter_count"),
        "last_successful_sync": data.get("last_successful_sync"),
        "sync_complete": data.get("sync_complete"),
        "skipped_meters": data.get("skipped_meters"),
        "incomplete_hours": data.get("incomplete_hours"),
        "unresolved_history_start": data.get("unresolved_history_start"),
    }
