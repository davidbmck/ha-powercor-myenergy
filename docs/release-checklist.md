# Release validation

Release [0.2.0](https://github.com/davidbmck/ha-powercor-myenergy/releases/tag/0.2.0)
was published with owner approval after
[all validation jobs passed](https://github.com/davidbmck/ha-powercor-myenergy/actions/runs/43851661592).
No HACS checks were ignored.

Verified:

- 62 standalone tests and 25 artifact-based HA/Recorder tests on each of
  HA 2026.10.4 and 2026.11.1.
- Reproducible flat release ZIP, matching manifest/tag, bundled client,
  translations, public CA, original icon and license notices.
- Fresh repository history and actual GitHub asset scanned for secrets and
  manually checked for identifying data; no old development refs imported.
- Actual previous manual build upgraded to the new artifact with synthetic
  configuration, credentials/session, sensor identities and stored history
  preserved across a process restart without another login.
- Published repository registration, ZIP download, runtime imports and repeated
  installation through the native Marketplace and legacy HACS 2.0.5 installer
  APIs. Reinstallation replaced stale component files; HA state outside the
  component folder remained intact. These were installer API checks, not a
  visual walkthrough of the store or GitHub authorisation screens.
- All disposable test containers and temporary remote files removed.

No live portal or mailbox credentials were used for release validation.
The seven-day renewal observation and earlier live authentication checks do
not establish long-term reliability. Default catalogue submission is separate.

For later releases, repeat the relevant offline/upgrade checks, validate the
manifest/tag and attached ZIP, run hassfest and HACS Action without ignored
checks, inspect the actual artifact for sensitive data, and obtain owner
approval before publishing. Every release must retain root `hacs.json`.
Never import refs or discussions from the private development archive.
