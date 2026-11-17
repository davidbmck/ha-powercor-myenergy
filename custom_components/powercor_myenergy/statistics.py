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
from .myenergy.model import ChannelKind
from .tariff import hourly_costs, interval_price, plan_at, request_dates

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
        entry = getattr(coordinator, "config_entry", None)
        settings = getattr(entry, "options", {}).get("tariff", {})
        if settings and settings["currency"] != hass.config.currency:
            raise HomeAssistantError("The configured currency changed; existing myEnergy costs cannot be relabelled")
        registers = {plan["controlled_load_register"]: ChannelKind.CONTROLLED_LOAD
                     for plan in settings.get("plans", []) if plan.get("controlled_load_register")}
        reading_options = {"channel_kinds": registers} if registers else {}
        fetch_start, fetch_end = request_dates(settings, start, end, datetime.now(AEST).date())
        readings = []
        skipped_meters = 0
        if meters is None:
            meters = await coordinator.async_authenticated_call(coordinator.client.get_meters)
        for meter in meters:
            cursor = fetch_start
            unavailable = False
            while cursor <= fetch_end:
                chunk_end = min(fetch_end, cursor + timedelta(days=29))
                try:
                    batch = await coordinator.async_authenticated_call(
                        coordinator.client.get_readings, meter, cursor, chunk_end, **reading_options,
                    )
                    readings.extend(batch)
                except DataUnavailable:
                    unavailable = True
                cursor = chunk_end + timedelta(days=1)
            skipped_meters += int(unavailable)

        try:
            hours = hourly_energy(readings)
            costs = hourly_costs(readings, hours, settings) if settings else {}
        except ValueError as err:
            raise HomeAssistantError(f"Historical intervals could not be priced or grouped: {err}") from err

        ids = {statistic_id(channel): UnitOfEnergy.KILO_WATT_HOUR for channel in hours}
        ids.update({cost_statistic_id(channel): settings["currency"] for channel, values in costs.items() if values})
        existing = {item["statistic_id"]: item for item in await async_list_statistic_ids(hass, statistic_ids=set(ids))} if ids else {}
        if any(item["source"] != DOMAIN or item["statistics_unit_of_measurement"] != ids[item["statistic_id"]]
               for item in existing.values()):
            raise HomeAssistantError("Existing statistics have incompatible source or units")

        requested_hours = ((fetch_end - fetch_start).days + 1) * 24
        unimported = sorted(statistic_id(channel) for channel in {r.channel for r in readings} - hours.keys())
        result = {
            "streams": {}, "cost_streams": {}, "skipped_meters": skipped_meters,
            "start_date": fetch_start.isoformat(), "end_date": fetch_end.isoformat(),
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
            rows, changed = await _async_reconcile(hass, stream_id, values)
            if rows:
                pending.append((metadata, rows))
            source = dict(values)
            result["streams"][stream_id] = {
                "hours": len(values), "kwh": float(sum(source.values())),
                "incomplete_hours": requested_hours - len(values),
                "changed_hours": len(changed),
            }
            result["incomplete_hours"] += requested_hours - len(values)
        for channel, values in costs.items():
            if not values:
                continue
            stream_id = cost_statistic_id(channel)
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE, has_sum=True,
                name=f"myEnergy {'export credit' if channel.kind.value == 'grid_export' else channel.kind.value.replace('_', ' ') + ' cost'} {channel.channel_id[:8]}",
                source=DOMAIN, statistic_id=stream_id, unit_class=None,
                unit_of_measurement=settings["currency"],
            )
            rows, changed = await _async_reconcile(hass, stream_id, values)
            if rows:
                pending.append((metadata, rows))
            result["cost_streams"][stream_id] = {
                "hours": len(values), "amount": float(sum(value for _, value in values)),
                "currency": settings["currency"], "changed_hours": len(changed),
            }
        expected_costs = sum(
            interval_price(plan_at(settings, hour), hour, channel.kind.value) is not None
            for channel, values in hours.items() for hour, _ in values
        ) if settings else 0
        result["unpriced_hours"] = expected_costs - sum(len(values) for values in costs.values())
        for metadata, rows in pending:
            async_add_external_statistics(hass, metadata, rows)
        if pending:
            # The import API queues writes; hold sync_lock until Recorder has
            # committed them so a second import observes the updated sums.
            await get_instance(hass).async_block_till_done()
        result["complete"] = bool(hours) and not skipped_meters and not unimported and result["incomplete_hours"] == 0 and result["unpriced_hours"] == 0
        return result


def cost_statistic_id(channel):
    prefix = "compensation" if channel.kind.value == "grid_export" else "cost"
    return f"{DOMAIN}:{prefix}_{channel.channel_id}_{channel.kind.value}"


async def _async_reconcile(hass, stream_id, values):
    """Upsert hourly amounts and repair later cumulative totals for either unit."""
    source = dict(values)
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
    changed = {hour for hour, amount in source.items()
               if hour not in old or not isclose(float(amount), old[hour]["state"], rel_tol=0, abs_tol=1e-9)}
    rows = []
    total = base
    first_changed = min(changed) if changed else None
    for hour in sorted(set(source) | set(old)):
        amount = source.get(hour)
        if amount is None:
            amount = Decimal(str(old[hour]["state"]))
        total += amount
        if first_changed is not None and hour >= first_changed:
            rows.append(StatisticData(start=hour, state=float(amount), sum=float(total)))
    return rows, changed
