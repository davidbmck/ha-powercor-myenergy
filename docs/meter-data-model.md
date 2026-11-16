# Normalised meter data

`myenergy.model` provides two frozen dataclasses:

- `Channel(meter_id, channel_id, kind)`: a stream of grid import, grid export,
  or controlled-load energy, viewed from the property. The client derives stable
  opaque IDs, keeping upstream identifiers private.
- `Reading(channel, start, end, kwh, quality=Quality.UNKNOWN)`: interval energy
  as a finite, nonnegative `Decimal` in kWh. Missing energy uses `None` with
  `Quality.MISSING`; zero means known zero consumption.

Quality labels are `ACTUAL`, `ESTIMATED`, `SUBSTITUTED`, `MISSING`, and `UNKNOWN`.
The channel ID pair and interval bounds identify a reading. A correction is a
revised value or quality for that same interval; reconciliation is separate.

## Time

Bounds must be timezone-aware and are converted to UTC. Start is inclusive,
end is exclusive, and duration is `end - start`. Use `Australia/Melbourne` for
local display. Callers supplying local times must resolve ambiguous times using
`fold` and reject nonexistent times.

These synthetic intervals each last 30 minutes:

| Case | UTC start → end | Melbourne start → end |
| --- | --- | --- |
| Spring jump | 2026-10-03 15:30 → 16:00 | 2026-10-04 01:30 +10:00 → 03:00 +11:00 |
| Autumn first 02:00 | 2026-04-04 15:00 → 15:30 | 2026-04-05 02:00 +11:00 → 02:30 +11:00 |
| Autumn second 02:00 | 2026-04-04 16:00 → 16:30 | 2026-04-05 02:00 +10:00 → 02:30 +10:00 |

The spring jump is not missing energy; the repeated autumn hour has distinct
UTC intervals. The client applies fixed AEST to NEM12 source days, following
AEMO detailed-data procedures and the observed 48-value transition-day exports.
See the [client contract](client.md) for this inference and date semantics.

## Example

Synthetic normal-day and missing readings:

```python
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from myenergy.model import Channel, ChannelKind, Quality, Reading

channel = Channel("meter-001", "channel-import", ChannelKind.GRID_IMPORT)
start = datetime(2026, 7, 1, tzinfo=timezone.utc)
end = start + timedelta(minutes=30)
actual = Reading(channel, start, end, Decimal("0.25"), Quality.ACTUAL)
missing = Reading(channel, end, end + timedelta(minutes=30), None, Quality.MISSING)
```
