"""Normalised electricity channels and interval energy readings."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum


class ChannelKind(str, Enum):
    """Energy direction from the property's perspective."""

    GRID_IMPORT = "grid_import"
    GRID_EXPORT = "grid_export"
    CONTROLLED_LOAD = "controlled_load"


@dataclass(frozen=True)
class Channel:
    """A stream identified by stable opaque IDs derived by the client."""

    meter_id: str
    channel_id: str
    kind: ChannelKind


class Quality(str, Enum):
    """Quality of one interval, independent of upstream quality codes."""

    ACTUAL = "actual"
    ESTIMATED = "estimated"
    SUBSTITUTED = "substituted"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Reading:
    """Energy in kWh for the half-open period [start, end)."""

    channel: Channel
    start: datetime
    end: datetime
    kwh: Decimal | None
    quality: Quality = Quality.UNKNOWN

    def __post_init__(self) -> None:
        for name in ("start", "end"):
            value = getattr(self, name)
            if value.utcoffset() is None:
                raise ValueError("Reading bounds must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        if self.end <= self.start:
            raise ValueError("Reading end must be after start")
        if (self.kwh is None) != (self.quality == Quality.MISSING):
            raise ValueError("Missing energy requires missing quality and vice versa")
        if self.kwh is not None:
            if not isinstance(self.kwh, Decimal):
                raise TypeError("Reading energy must be Decimal kWh or None")
            if not self.kwh.is_finite() or self.kwh < 0:
                raise ValueError("Reading energy must be finite and nonnegative")
