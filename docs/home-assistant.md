# Home Assistant setup and recovery

See the [README](../README.md) for Marketplace/HACS installation and migration.
For development, install from a checkout into the HA configuration directory:

```sh
python tools/install_integration.py /path/to/ha-config
```

The installer bundles the single maintained `myenergy` client source with the
component. Restart HA after first installation, then add **Powercor / CitiPower
myEnergy** under **Settings → Devices & services**. Supply the myEnergy login
and choose manual email verification or optional automatic verification.
For automatic verification, use a dedicated IMAP inbox receiving
the codes directly; Gmail requires an app password. The sender defaults to the
observed service address, `no-reply@powercor.com.au`; enter the recipient address
exactly as it appears in the verification email's To header. The server, mailbox
login, sender and subject defaults are editable. A separate mailbox login and
non-Gmail IMAP providers using verified TLS on port 993 are supported by the
configuration; live checks have only established the documented Gmail path.
Credentials and reusable
session state are retained in HA config entries. They are not encrypted at rest.

Manual verification is available during setup and reauthentication. That mode
requires intervention when the session expires. Failed portal credentials or
rejected mailbox credentials start HA's reauthentication flow, where automatic
or manual verification can be selected again. OTP values are never persisted.

The [completed session investigation](authentication-session.md)
demonstrated seven days of access with 80-minute checks, fresh context, cookie
adoption and renewal. It did not establish three months of access, so this
integration also supports fresh login and automatic IMAP verification when
renewal fails. A refresh makes at most one login attempt; a persisted 15-minute
cooldown prevents setup retries or restarts from repeatedly requesting codes.
Temporary network/mailbox failures retry through HA; unexpected service changes
remain distinct from authentication expiry and do not trigger password retries.

The diagnostic last-update sensor records the last successful protected refresh.
Its attributes report the last successful historical retrieval, whether the
requested history is complete, skipped meters, incomplete hours and the start
of any unresolved older period. The latest-source sensor shows the end of the
newest non-missing source interval.
The coordinator refreshes the portal session every 80 minutes. Historical sync
is due six hours after the last successful retrieval, including partial results.
Its first backfill requests the preceding 30 complete AEST days; later requests
cover at least 14 days through
yesterday and extend back after an outage, up to a 30-day automatic limit.
Partial retrievals retain their original start date for later retries. If an
unresolved period ages beyond the automatic limit, diagnostics report its start
date for manual import; normal recent reconciliation continues. A complete
manual import covering the older period clears that marker. Missing readings
do not block session maintenance. Diagnostics contain only operational status,
without customer identifiers, credentials, sessions or readings. The bundled public intermediate
certificate supplies the chain omitted by the observed portal; normal TLS and
hostname verification remain enabled.

## Historical statistics import

With the integration loaded and Recorder enabled, run the
`powercor_myenergy.import_history` action under **Developer tools → Actions**
to backfill older dates or reconcile a selected period.
Choose inclusive `start_date` and `end_date` in NEM fixed AEST; both dates must
be before the current AEST date. If more than one myEnergy entry is loaded,
provide its `config_entry_id`. The action returns an hour count, kWh total, changed-hour count and
incomplete-hour count for each imported stream. It also reports how many meters
had unavailable data and lists streams with no complete hours. The action waits
for Recorder to commit queued writes before returning.

The action creates separate external statistics for each discovered grid import
and export stream, in kWh. Each UTC-hour row contains that hour's energy and a
running sum. An hour with absent or missing intervals is omitted; no zero is
invented. Each returned kWh total covers only complete imported hours, so
compare against the same complete hours in the source data. AEST days retain
24 hours across Melbourne daylight-saving changes.

Under **Settings → Dashboards → Energy**, choose the new myEnergy grid import
statistics as **Grid consumption** and any myEnergy grid export statistics as
**Return to grid**. If there are multiple streams of a direction, select each
one that belongs in the total. Repeating an import with unchanged source data
does not queue new rows. Revised or newly available complete hours are inserted
and later cumulative sums are adjusted. Hours that become incomplete are left
untouched because Recorder has no per-hour deletion through this import API.

## Disposable Docker validation

On a Docker host with the HA image already installed:

```sh
python tools/test_ha.py
python tools/test_ha.py --live secrets.json
```

The first command builds the release ZIP and runs focused offline HA boundary
tests from its extracted contents, without network access.
The second also exercises the real HA configuration flow, fresh automatic login,
forced loss of the local session, recovery, a one-day historical import, the
diagnostic sensor and restart with persisted session state. The historical day
defaults to 30 AEST days ago; an optional `history_date` in the private settings
selects another known complete date. The check requires complete coverage and
compares source intervals independently with Recorder's hourly energy changes,
including the first hour. `--live -` reads the settings from stdin.
Use `--image` to select an installed image; the runner does not pull images.

Each run has a separate container, no production HA mounts, devices or ports,
and temporary HA storage in memory. The container and its state are removed on
completion, failure or a handled interruption. Live checks report outcomes only.
They supplement the deterministic tests and do not establish long-term service
reliability.

Release 0.2.0 passed 62 standalone tests and 25 artifact-based HA/Recorder tests
on both HA 2026.10.4 and 2026.11.1. A previous manual build was upgraded with
synthetic configuration and history preserved. Native Marketplace and legacy
HACS installer checks downloaded and installed the published ZIP, then replaced
it successfully. See the [release validation record](release-checklist.md).

Earlier private live checks exercised fresh manual/automatic authentication,
lost-session recovery, historical daily energy compared with source intervals,
and restart with session reuse. They were not repeated for this release and
do not establish long-term unattended reliability.
