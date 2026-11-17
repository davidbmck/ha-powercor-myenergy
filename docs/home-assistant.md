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

## Usage costs and export credits

HA disables its native **Use a static price** option for external energy
statistics: that calculation needs a sensor entity. Instead, open this
integration's **Configure** options in **Settings → Devices & services**.
Enter the import price and optional export credit in HA's configured currency
per kWh, then save. For AUD, enter `0.30` for 30 c/kWh. A flat-rate setup takes
one form submission. Zero is a valid price; leave unused export pricing blank.

To raise prices or change provider, reopen Configure. Current prices are
prefilled, including any peak, shoulder, overnight and second-block prices.
Change the amounts and **New prices start on**, then save. Hours and allowances
stay unchanged. The date defaults to tomorrow, or the day after the last
scheduled change. Earlier readings keep their saved prices, even when they
arrive late. The first plan covers all historical imports; enter historical
changes in date order if reconstructing earlier bills.

Enable **Change plan type, hours or special settings** only when those details
change. Presets provide editable examples, with no assumed retailer prices:

| Import model | Example periods |
| --- | --- |
| Flat | One price all day |
| Peak / off-peak | Peak 16:00–21:00 daily; off-peak otherwise |
| Peak / shoulder / off-peak | Weekday peak 15:00–21:00; shoulder 07:00–22:00 outside peak, including weekends |
| Free midday | Free 11:00–14:00; standard price otherwise; edit the end to 15:00 for a four-hour window |
| Midday Power Saver | Free 11:00–14:00, solar soak 14:00–16:00, peak 16:00–21:00, off-peak otherwise |
| Overnight / EV | Cheap 00:00–06:00; standard price otherwise |
| Two blocks | First Y kWh at the import price, remainder at the second price |

Match your bill's hours, weekdays and clock: Victorian local time follows DST;
fixed AEST does not. Windows end exclusively and may wrap midnight. Free,
peak and shoulder are checked in that order, allowing peak inside a broader
shoulder window. An optional extra overnight window overrides them, so keep it
separate from free hours. Five-minute time boundaries are accepted, but a
boundary inside a longer source interval is rejected rather than estimating
how energy was split. Different summer/winter prices can use dated plans.

Export credits support a fixed rate or two blocks. Block allowances reset at
local midnight daily, or every one/three calendar months from the entered
billing-cycle date. Month-end anchors clamp without drifting. An allowance is
shared across one meter's channels of the same direction; separate meters do
not pool allowances. Price changes keep counting usage from the cycle start.
Block import pricing cannot also use time bands. This is a two-block model,
not a free-window usage cap or a multi-tier billing engine.

Optional controlled-load pricing requires an explicitly confirmed NEM12
register (E2–E9); obtain it from your export or distributor. No register is
assumed to be controlled load. The general import rate never prices that
channel. A separate controlled-load price is flat per kWh.

After an import, select the matching **myEnergy … cost** statistic using
**Use an entity tracking the total costs** in Energy grid consumption settings,
and **myEnergy export credit …** for return-to-grid compensation. These are
external statistics despite HA's option wording. Select each matching pair
when there are multiple streams. Credits are positive amounts, which HA
subtracts from import costs.

Costs use the original interval kWh and timestamp before grouping complete
UTC hours; they are not calculated from today's sensor state. Reimporting
unchanged readings, including after restart, adds no costs. Corrections repair
later running sums. A block import retrieves the affected billing cycles in
requests of at most 30 AEST days, capped at yesterday, so it can exceed the
normal rolling window. Missing earlier cycle data withholds costs whose block
position is unknown; the `unpriced_hours` diagnostic reports these complete
energy hours. Previously imported hours that become incomplete stay untouched.

Upgrading from 0.2.0 preserves the existing entry, credentials and energy
statistics; no entry recreation or data migration is needed. Adding pricing is
optional. Saving prices requests a recent reconciliation. To cost already imported
older history or apply an intentional historical correction, run the history
import action for those dates; the original intervals must still be available
from myEnergy. Choose **Correct prices in a saved historical
period** to edit a saved plan; it requires confirmation and preserves its
original boundaries. Later saved plans stay intact. Keep previously enabled
export/controlled-load pricing when correcting a period; use zero for a
zero-priced period. Existing money statistics cannot be relabelled if HA's
currency changes; restore that currency before importing or editing prices.

These statistics track usage costs and export credits. They exclude daily
supply fees, demand charges, discounts, wholesale prices, public-holiday
calendars, and retailer-specific caps. They are not a full bill calculation.
Presets are based on published [Victorian Midday Power Saver](https://www.energy.vic.gov.au/households/save-energy-and-money/victorian-midday-power-saver),
[flexible pricing](https://www.energyaustralia.com.au/what-are-flexible-pricing-plans),
and [overnight EV](https://help.ovoenergy.com.au/electric-vehicles/) examples;
retailer periods and conditions vary. See the government's
[tariff guide](https://www.energy.gov.au/solar/financial-benefits-solar/electricity-pricing-plans-and-tariffs)
for the distinction between usage and other charges.

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

Cost tracking passed 73 standalone tests and 33 artifact-based HA/Recorder
checks on HA 2026.10.4 and 2026.11.1. An offline upgrade from the published
0.2.0 ZIP preserved the config entry, credentials and every synthetic energy
row, added retrospective import costs/export credits, and survived a further
restart with repeated imports unchanged. These checks used no live account.

Release 0.2.0 passed 62 standalone tests and 25 artifact-based HA/Recorder tests
on both HA 2026.10.4 and 2026.11.1. A previous manual build was upgraded with
synthetic configuration and history preserved. Native Marketplace and legacy
HACS installer checks downloaded and installed the published ZIP, then replaced
it successfully. See the [release validation record](release-checklist.md).

Earlier private live checks exercised fresh manual/automatic authentication,
lost-session recovery, historical daily energy compared with source intervals,
and restart with session reuse. They were not repeated for this release and
do not establish long-term unattended reliability.
