"""Minimal NEM12 energy parser, with fixed AEST interval-ending semantics."""

from collections.abc import Mapping
import csv
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import re

from .errors import DataUnavailable, UnexpectedResponse
from .model import Channel, ChannelKind, Quality, Reading

AEST = timezone(timedelta(hours=10), "AEST")


def identifier(*parts):
    """Stable opaque identity; hashes are still private customer metadata."""
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def quality(code):
    if code == "A":
        return Quality.ACTUAL
    if re.fullmatch(r"E[0-9]{2}", code):
        return Quality.ESTIMATED
    if re.fullmatch(r"[SF][0-9]{2}", code):
        return Quality.SUBSTITUTED
    raise UnexpectedResponse("unsupported_interval_quality")


def parse_nem12(text, *, nmi, start, end, channel_kinds=None):
    """Read inclusive AEST dates, converting interval energy to UTC/kWh.

    AEMO detailed-data procedures specify AEST. This convention is applied
    to the portal's NEM12 export; no Melbourne DST shift is applied to rows.
    Unknown suffixes need an explicit mapping instead of guessing a tariff.
    Empty values are rejected, never converted to zero or fabricated gaps.
    """
    kinds = {"E1": ChannelKind.GRID_IMPORT, "B1": ChannelKind.GRID_EXPORT}
    if channel_kinds is not None:
        if not isinstance(channel_kinds, Mapping) or any(
            not isinstance(k, str) or not isinstance(v, ChannelKind)
            for k, v in channel_kinds.items()
        ):
            raise ValueError("Channel mappings require suffixes and ChannelKind values")
        if any(key in kinds and value != kinds[key] for key, value in channel_kinds.items()):
            raise ValueError("E1/B1 energy directions cannot be changed")
        kinds.update(channel_kinds)
    # Discovery supplies an 11-character NMI including its check digit;
    # observed NEM12 records carry the 10-character NMI without that digit.
    source_nmi = nmi[:-1] if len(nmi) == 11 else nmi
    readings, seen = [], set()
    channel = None
    pending = None
    header = finished = False

    def finish_day():
        if pending is None:
            return
        day, values, qualities = pending
        if any(value is None for value in qualities):
            raise UnexpectedResponse("incomplete_interval_quality_events")
        if start <= day <= end:
            midnight = datetime.combine(day, time(), AEST)
            for index, (value, flag) in enumerate(zip(values, qualities)):
                left = midnight + index * duration
                key = (channel.channel_id, left)
                if key in seen:
                    raise UnexpectedResponse("duplicate_interval")
                seen.add(key)
                readings.append(Reading(channel, left, left + duration, value * scale, flag))

    try:
        for row in csv.reader(io.StringIO(text), strict=True):
            if not row:
                continue
            tag = row[0]
            if finished:
                raise UnexpectedResponse("records_after_nem12_end")
            if not header:
                if tag != "100" or len(row) < 2 or row[1] != "NEM12":
                    raise UnexpectedResponse("invalid_nem12_header")
                header = True
                continue
            if tag in ("200", "300", "900"):
                finish_day()
                pending = None
            if tag == "200":
                if len(row) < 9 or row[1] != source_nmi or any(not row[i] for i in (3, 4, 6)):
                    raise UnexpectedResponse("invalid_nem12_stream")
                if row[4] not in kinds:
                    raise UnexpectedResponse("unmapped_energy_channel")
                scale = {"KWH": Decimal(1), "WH": Decimal("0.001")}.get(row[7].upper())
                minutes = int(row[8])
                if scale is None or minutes not in (5, 15, 30, 60):
                    raise UnexpectedResponse("unsupported_energy_unit_or_interval")
                count, duration = 1440 // minutes, timedelta(minutes=minutes)
                meter_id = identifier("meter", source_nmi, row[6])
                channel = Channel(meter_id, identifier("channel", meter_id, row[3], row[4], row[5]), kinds[row[4]])
            elif tag == "300":
                if channel is None or len(row) < count + 6 or not re.fullmatch(r"[0-9]{8}", row[1]):
                    raise UnexpectedResponse("invalid_nem12_day")
                day = datetime.strptime(row[1], "%Y%m%d").date()
                values = [Decimal(value) for value in row[2:count + 2]]
                if any(not value.is_finite() or value < 0 for value in values):
                    raise UnexpectedResponse("invalid_interval_energy")
                code = row[count + 2]
                flags = [None] * count if code == "V" else [quality(code)] * count
                pending = (day, values, flags)
            elif tag == "400":
                if pending is None or len(row) < 4:
                    raise UnexpectedResponse("invalid_interval_quality_event")
                first, last = int(row[1]), int(row[2])
                flags = pending[2]
                if not 1 <= first <= last <= count or any(flag is not None for flag in flags[first - 1:last]):
                    raise UnexpectedResponse("overlapping_or_invalid_quality_event")
                flags[first - 1:last] = [quality(row[3])] * (last - first + 1)
            elif tag == "900":
                finished = True
            else:
                raise UnexpectedResponse("unsupported_nem12_record")
        if not header or not finished:
            raise UnexpectedResponse("incomplete_nem12_file")
    except (ValueError, TypeError, IndexError, InvalidOperation, csv.Error, OverflowError):
        raise UnexpectedResponse("invalid_nem12_data") from None
    if not readings:
        raise DataUnavailable("no_interval_data_in_requested_period")
    return sorted(readings, key=lambda reading: (reading.start, reading.channel.channel_id))
