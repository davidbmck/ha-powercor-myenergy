# Standalone client

`myenergy.MyEnergyClient` uses a caller-owned `aiohttp.ClientSession`. Install
`requirements.txt`. Use one client and a dedicated session per account; the
caller closes the session and provides TLS configuration. Operations are
serialized because authenticated bootstrap consumes a temporary token cookie.

```python
from datetime import date
import aiohttp
from myenergy import MyEnergyClient

async def read_period(saved_state):
    async with aiohttp.ClientSession() as session:
        client = MyEnergyClient(session)
        client.restore_state(saved_state)
        meters = await client.get_meters()
        readings = await client.get_readings(
            meters[0], date(2026, 1, 1), date(2026, 1, 2)
        )
        return readings, client.export_state()
```

For a fresh login, call `await client.login(email, password,
imap_config=config)`. It coordinates the proven password, email OTP and
completion sequence. The IMAP mapping and restrictions are described in the
[login note](direct-login.md#automatic-imap-code-retrieval). For manual OTP,
call `start_login(email, password)`, then `submit_otp(code)`. Calling `login`
without IMAP requests a code and raises `ChallengeRequired`, ready for
`submit_otp`. No credentials, OTP or mailbox configuration enter exported state.

`export_state()` returns a versioned JSON-compatible cookie snapshot, including
scope and absolute expiry. Restore it into a fresh client, then request data;
the client refreshes Aura context, renews the session, updates the timer and
independently confirms it before each data operation. The diagnostic timer read
before renewal is omitted. Cookies rotated by the service are included
in the next export. Persist the result after successful operations. Expired
sessions require a new login; there is no background renewal or automatic
password retry. Pending challenges cannot be persisted.

The caller owns private storage. Session state, meter metadata, opaque IDs and
readings remain sensitive. Do not log them. Development file handling stays in
`tools.probe_login`; production credentials use Home Assistant config entries.

`get_meters()` returns supply-point access metadata, retaining only the NMI and
company needed for requests. NEM12 NMIs omit discovery’s check digit. Its `id` is a stable opaque supply ID. Physical
meter/channel IDs are derived from NEM12 identifiers when parsing readings.
They do not depend on dates, values or quality, so corrected readings retain
identity. A changed physical meter gets a different meter ID.

`get_readings(meter, start, end)` accepts inclusive **AEST dates**, returning
`Reading` objects in UTC with `Decimal` interval energy in kWh. NEM12 uses
interval-ending positions; readings expose half-open start/end bounds. The
parser uses fixed AEST (UTC+10), following AEMO detailed-data procedures. The
portal returned 48 half-hour values on both Victorian DST transition dates in
a live check; interpreting the export as AEST is an inference from that shape
and the standard, rather than an explicit portal timezone declaration. Convert
to `Australia/Melbourne` for display; do not shift source rows for DST. See the
[AEMO detailed-data procedure](https://aemo.com.au/-/media/files/electricity/nem/retail_and_metering/metering-procedures/2021/metering-data-provision-procedures-v20.pdf?la=en)
and [NEM12 format](https://www.aemo.com.au/-/media/files/stakeholder_consultation/consultations/nem-consultations/2024/2024-metering-services-review-package-1/final-stage/mdff-specification-nem12-nem13-v27.pdf).

The observed portal rejects equal date bounds. For a single day, the client
requests through the following day and filters the result. Missing dates are
left absent; blanks, malformed values and unknown quality flags are rejected,
never replaced with zero. Quality `V` requires complete, nonoverlapping `400`
events. Supported energy units are Wh/kWh and intervals are 5/15/30/60 minutes.
`E1` means property import and `B1` property export. Other suffixes require an
explicit `channel_kinds={"E2": ChannelKind.CONTROLLED_LOAD}` mapping; controlled
load is not inferred from a suffix alone. Conflicting `E1`/`B1` overrides and
non-mapping inputs raise `ValueError`.

Client failures derive from `MyEnergyError`: `AuthenticationExpired`,
`AuthenticationError` (credentials/OTP rejection), `ChallengeRequired`,
`TransportError`, `AccessDenied`, `UnexpectedResponse`, `DataUnavailable`, and
`MailboxError`. Messages contain fixed outcomes, without portal payloads.
Cancellation waits for bounded IMAP work to finish before closing the mailbox.
Incomplete discovery beyond the observed 50-assignment limit raises
`DataUnavailable`. Unexpected record types or protocol changes fail explicitly.

The ordinary tests use synthetic responses and no network. Live validation
covers one existing account, direct login, session reload, protected discovery
and interval parsing; it does not establish long-term session reliability.
See [HA setup and recovery](home-assistant.md) for configuration and storage.
