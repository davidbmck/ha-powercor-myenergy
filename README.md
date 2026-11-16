# Powercor / CitiPower myEnergy

![Independent myEnergy integration](custom_components/powercor_myenergy/brand/icon.png)

Home Assistant integration for retrospective electricity readings from the
**CitiPower / Powercor myEnergy** portal in Victoria, Australia. Imports grid
consumption and solar export into the Energy dashboard. This is interval data,
not real-time power monitoring.

**Release preparation:** the first public release is not published yet.
Marketplace and HACS installation becomes available after publication.

## Install and set up

Requires Home Assistant **2026.10.4 or newer** and a myEnergy account with access
to the supply point. The built-in Marketplace requires HA 2026.11 or newer;
legacy HACS can use the same release ZIP.

1. In **Settings → Marketplace**, add
   [this repository](https://github.com/davidbmck/ha-powercor-myenergy) from its
   link and install its latest release. In legacy HACS, add that URL under
   **Custom repositories**, category **Integration**, then download it.
2. Restart Home Assistant if requested. Open **Settings → Devices & services →
   Add integration**, then select **Powercor / CitiPower myEnergy**.
3. Enter your myEnergy email and password. Choose manual verification to type
   the emailed code, or enable automatic verification with a dedicated IMAP
   mailbox receiving the codes directly.

[![Set up myEnergy](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=powercor_myenergy)

For automatic verification, configure the editable IMAP server, mailbox login,
password, folder, exact sender/recipient and subject template. The mailbox login
can differ from the myEnergy address. Gmail needs an app password; other
providers must support verified TLS on port 993. Use a dedicated mailbox and a
revocable app password. Codes remain transient, but credentials and reusable
session state are stored in HA config entries **without encryption at rest**.
Protect HA configuration files and backups.

## Energy dashboard and history

After startup, the integration backfills the preceding **30 complete AEST
days**. It maintains the session every 80 minutes and retrieves history every
six hours, reconciling at least 14 recent days. Delayed or corrected complete
hours update existing statistics without double-counting. Automatic recovery
after outages is capped at 30 days; older gaps require manual import.

Under **Settings → Dashboards → Energy**, select the myEnergy grid-import
statistics as **Grid consumption**, and export statistics as **Return to grid**.
Select each applicable stream if the account has more than one. The diagnostic
sensors report the last successful update, latest source timestamp and gaps.

For older dates, run **Powercor / CitiPower myEnergy: Import historical readings**
under **Developer tools → Actions**. Enter inclusive completed dates in fixed
**AEST (UTC+10)**, including during Victorian daylight saving. See
[history and recovery](docs/home-assistant.md) for action fields and results.

Incomplete hours are omitted rather than filled with zero. An hour previously
imported that later becomes incomplete remains in Recorder. Controlled-load
suffixes are not inferred automatically. Tariffs and billing are outside this
integration's scope.

## Updates and migration

Install updates through Marketplace or HACS, then restart HA. Keep a backup
before updating. If setup asks for reauthentication, update the existing entry;
manual and automatic verification remain available.

For an existing manual installation, back up HA, install the release over the
existing `custom_components/powercor_myenergy` folder, then restart. **Keep the
existing integration entry.** Do not add it again or remove its statistics.
The domain, config-entry schema, sensor unique IDs and external statistic IDs
are retained, so the repository change does not require a data migration.
Remove any obsolete store registration for the same domain before registering
this repository; two stores/repositories must not manage the same folder.

## Troubleshooting and support

- **No recent readings:** myEnergy data is retrospective. Check portal
  availability and the diagnostic sensors; an unresolved older gap can need a
  manual history import.
- **Manual verification requested:** complete reauthentication in Devices &
  services. Automatic login makes one bounded attempt, with a persisted
  15-minute cooldown before another attempt.
- **Mailbox failure:** check the separate mailbox login, app password, exact
  email headers, subject and IMAP access. Avoid concurrent browser logins or
  code resends while verification is pending.
- **Unexpected portal response:** check for an integration update and report
  the fixed error outcome. Do not share full portal responses or debug dumps.

Report reproducible problems in [GitHub issues](https://github.com/davidbmck/ha-powercor-myenergy/issues)
with HA/integration versions, safe error messages and redacted diagnostics.
Never attach credentials, OTPs, cookies, account/NMI/meter IDs, addresses,
raw captures or actual interval readings.

The observed renewal approach completed a seven-day trial. Short manual and
automatic login/recovery checks passed during development; long-term
unattended reliability and every account/challenge type have not been
established. The portal uses undocumented behaviour that can change.

## Development

Install `requirements.txt`, then run `python -m unittest discover -s tests`.
`python tools/build_release.py --tag 0.2.0` produces the self-contained release
ZIP. `python tools/test_ha.py --image <installed-image>` runs offline HA and
Recorder checks from that artifact in a disposable Docker container. See
[developer validation](docs/home-assistant.md#disposable-docker-validation).

The standalone [client](docs/client.md) owns portal/authentication parsing;
the [meter model](docs/meter-data-model.md) has no HA dependencies. The HA
adapter owns config entries and statistics. Protocol notes cover
[interval exports](docs/interval-data-capture.md),
[sessions](docs/authentication-session.md) and [direct login](docs/direct-login.md).

Licensed under [MIT](LICENSE). See [NOTICE](NOTICE) for the public DigiCert
intermediate certificate and original icon provenance.

This independent community project is not affiliated with or endorsed by
CitiPower, Powercor, Salesforce or Home Assistant.
