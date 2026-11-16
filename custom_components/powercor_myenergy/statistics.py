"""Import and reconcile myEnergy's hourly external statistics."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import isclose

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics, async_list_statistic_ids, get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN
from .hourly import hourly_energy
from .myenergy import DataUnavailable

AEST = timezone(timedelta(hours=10), "AEST")


def statistic_id(channel):
    return f"{DOMAIN}:energy_{channel.channel_id}_{channel.kind.value}"


async def async_import_history(hass, coordinator, start, end, *, meters=None):
    """Queue complete UTC hours for inclusive, historical NEM/AEST dates.

    Recorder upserts hourly rows. Recompute all later sums when a source hour
    changes so previously stored history remains a consistent cumulative stream.
    """
    if end < start or end >= datetime.now(AEST).date():
        raise HomeAssistantError("Choose an ordered, fully historical AEST date range")
    if hass.state != CoreState.running:
        raise HomeAssistantError("Wait for Home Assistant and Recorder to finish starting")

    async with coordinator.sync_lock:
        readings = []
        skipped_meters = 0
        if meters is None:
            meters = await coordinator.async_authenticated_call(coordinator.client.get_meters)
        for meter in meters:
            try:
                batch = await coordinator.async_authenticated_call(
                    coordinator.client.get_readings, meter, start, end,
                )
            except DataUnavailable:
                skipped_meters += 1
                continue
            readings.extend(batch)

        try:
            hours = hourly_energy(readings)
        except ValueError as err:
            raise HomeAssistantError("Historical intervals could not be grouped into UTC hours") from err

        ids = {statistic_id(channel) for channel in hours}
        existing = {item["statistic_id"]: item for item in await async_list_statistic_ids(hass, statistic_ids=ids)} if ids else {}
        if any(item["source"] != DOMAIN or item["statistics_unit_of_measurement"] != UnitOfEnergy.KILO_WATT_HOUR
               for item in existing.values()):
            raise HomeAssistantError("Existing statistics have incompatible source or units")

        requested_hours = ((end - start).days + 1) * 24
        unimported = sorted(statistic_id(channel) for channel in {r.channel for r in readings} - hours.keys())
        result = {
            "streams": {}, "skipped_meters": skipped_meters,
            "unimported_streams": unimported,
            "latest_source_timestamp": max((r.end for r in readings if r.kwh is not None), default=None),
            "incomplete_hours": 0,
        }
        if result["latest_source_timestamp"] is not None:
            result["latest_source_timestamp"] = result["latest_source_timestamp"].isoformat()
        pending = []
        for channel, values in hours.items():
            stream_id = statistic_id(channel)
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"myEnergy {channel.kind.value.replace('_', ' ')} {channel.channel_id[:8]}",
                source=DOMAIN,
                statistic_id=stream_id,
                unit_class=EnergyConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            source = {hour: energy for hour, energy in values}
            first = values[0][0]
            recorder = get_instance(hass)
            stored = await recorder.async_add_executor_job(
                statistics_during_period, hass, first, None, {stream_id}, "hour", None, {"state", "sum"},
            )
            previous = stored.get(stream_id, [])
            old = {datetime.fromtimestamp(row["start"], timezone.utc): row for row in previous}
            if previous:
                base = Decimal(str(previous[0]["sum"])) - Decimal(str(previous[0]["state"]))
            else:
                last = await recorder.async_add_executor_job(
                    get_last_statistics, hass, 1, stream_id, False, {"sum"},
                )
                base = Decimal(str(last[stream_id][0]["sum"])) if last.get(stream_id) else Decimal(0)
            timeline = sorted(set(source) | set(old))
            changed = {hour for hour, energy in source.items()
                       if hour not in old or not isclose(float(energy), old[hour]["state"], rel_tol=0, abs_tol=1e-9)}
            rows = []
            total = base
            first_changed = min(changed) if changed else None
            for hour in timeline:
                energy = source.get(hour)
                if energy is None:
                    energy = Decimal(str(old[hour]["state"]))
                total += energy
                if first_changed is not None and hour >= first_changed:
                    rows.append(StatisticData(start=hour, state=float(energy), sum=float(total)))
            if rows:
                pending.append((metadata, rows))
            result["streams"][stream_id] = {
                "hours": len(values), "kwh": float(sum(source.values())),
                "incomplete_hours": requested_hours - len(values),
                "changed_hours": len(changed),
            }
            result["incomplete_hours"] += requested_hours - len(values)
        for metadata, rows in pending:
            async_add_external_statistics(hass, metadata, rows)
        if pending:
            # The import API queues writes; hold sync_lock until Recorder has
            # committed them so a second import observes the updated sums.
            await get_instance(hass).async_block_till_done()
        result["complete"] = bool(hours) and not skipped_meters and not unimported and result["incomplete_hours"] == 0
        return result
