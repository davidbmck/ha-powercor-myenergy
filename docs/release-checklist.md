# First public release

The repository starts from reviewed source with a fresh Git history. Previous
development history, discussions and private audit evidence are not included.
Only GitHub no-reply attribution is used for its commits.

Before publication:

- Run standalone and artifact-based HA/Recorder tests on the declared minimum
  and current stable versions. Validate manual and automatic setup, separate
  mailbox credentials, reauthentication, restart and upgrade with existing
  configuration and statistics.
- Require passing hassfest and HACS Action without ignored checks. Inspect the
  generated ZIP, manifest/tag alignment, certificate and original brand icon.
- Re-scan every reachable ref and the actual artifact with a secret scanner
  and manually inspect for identifiers, private infrastructure and readings.
- Obtain owner approval before making the repository public or publishing a
  GitHub release. Publish tag `0.2.0` with `powercor_myenergy.zip` attached;
  every release must retain root `hacs.json`.
- Verify clean installation and update from the published Marketplace/HACS
  repository link. Remove the preparation notice from README only when this
  path is available and tested.

Default catalogue submission is a separate task and does not delay the first
installable public release. Keep the previous repository private; never merge
or push its refs into this repository.
