# AGENTS.md

Guidance for coding agents and automated contributors working on this repository.

## Project principles

Keep the project small, understandable, and easy to change.

Strongly prefer straightforward routines and small data structures. Implement the current requirement with the fewest useful layers. Do not add managers, registries, factories, generic adapters, dependency injection, or extension hooks for possible future needs. Add an abstraction only when it makes the current code materially clearer or removes real duplication.

The intended dependency direction is:

```text
myEnergy service
      ↓
standalone client
      ↓
normalised meter model
      ↓
Home Assistant integration
      ↓
HA statistics / Energy dashboard
```

Do not bypass these boundaries for convenience. They describe responsibilities, not a requirement for a separate class, framework, or package at every step.

In particular:

- Do not put Salesforce/myEnergy request parsing into Home Assistant entities.
- Do not put Home Assistant objects into the standalone client.
- Keep authentication/session handling separate from meter-data transformation where practical.
- Use a plain function before adding a class when state or reuse does not require one.

## Work from the issue

Keep changes focused on the GitHub issue being implemented.

Before adding a new abstraction, feature, compatibility layer, or dependency, ask whether the current issue actually requires it.

Do not quietly expand an issue into adjacent features.

If implementation reveals a worthwhile follow-up that is not required to complete the current issue, document it for a separate issue instead.

## User data and secrets

Treat all data obtained from myEnergy as potentially sensitive, including interval readings, precise usage patterns, dates, and screenshots showing customer details.

Never commit real or unredacted:

- passwords;
- OTP codes;
- cookies;
- session tokens;
- authentication headers;
- raw HAR files;
- account numbers;
- NMI or meter identifiers;
- street addresses;
- email addresses;
- unsanitised API responses containing customer information;
- interval readings or distinctive usage traces;
- screenshots containing personal data.

Use synthetic or carefully sanitised values in:

- tests;
- fixtures;
- examples;
- documentation;
- logs;
- issue comments.

Do not assume an identifier is harmless simply because it is not a password.

If sensitive material is exposed, stop sharing it, remove it from the change, and tell the user so credentials or tokens can be rotated when needed. When credential handling is implemented, use Home Assistant's established configuration mechanisms rather than a custom plaintext store.

### HTTP captures

Authenticated browser/network captures are development inputs, not repository artifacts.

When using a real capture:

1. inspect it locally;
2. identify only the fields needed to understand the protocol;
3. create a minimal sanitised or synthetic fixture;
4. keep the raw capture outside the repository and delete it after deriving the sanitised shape.

Never add a full authenticated HAR to Git. Do not paste raw requests or responses into issues, PRs, or shared command output. The .gitignore is a backup guardrail, not a privacy check.

For development browser sessions, let the user complete login and 2FA. Do not record credentials or session material.

### Infrastructure details in shared text

Do not include real server names, hostnames, SSH aliases, private IP addresses, local usernames, or machine-specific paths in code comments, documentation, examples, commit messages, issue comments, or pull request text. Use role descriptions such as "the trial host" or "the local workstation", and synthetic paths where needed. Public upstream service domains required to explain the protocol are fine. Include private infrastructure details in shared text only when the user explicitly requests those exact details.

Before publishing, check prose as well as code and fixtures for identifying infrastructure details. Keep operational connection details in private local configuration.

### Logging and diagnostics

Logs should describe what happened without exposing secrets.

Good:

```text
Authentication session expired
Received 48 interval readings
Unexpected response from interval-data endpoint
```

Bad:

```text
Cookie: sid=...
NMI: <real NMI>
Login response: {complete customer object...}
```

Home Assistant diagnostics must redact authentication material, unnecessary customer identifiers, and full portal payloads.

## Testing

Use lightweight, targeted tests.

Tests should primarily protect:

- parsing of upstream responses;
- the normalised meter-data model;
- units and timestamp handling;
- Australian DST edge cases;
- idempotent imports;
- historical reconciliation;
- authentication/session state transitions where practical.

Prefer a few representative fixtures over a large matrix of nearly identical test cases. Do not write tests that merely repeat the implementation.

Do not create elaborate test harnesses, fake service frameworks, dependency-injection systems, or exhaustive tests unless the behaviour genuinely warrants them.

A change should usually add tests when it:

- fixes a reproducible bug;
- introduces non-trivial parsing or transformation;
- handles a meaningful edge case;
- changes reconciliation/statistics behaviour.

Simple plumbing does not need tests solely for coverage.

### Live services

The ordinary test suite must not require:

- a real myEnergy account;
- Home Assistant credentials;
- Gmail access;
- OTP delivery;
- internet access.

Authenticated manual checks, when genuinely needed, should use user-led login/2FA and must not leave credentials or real meter data in the repository or CI.

Live/manual validation may supplement tests but must not replace useful deterministic tests.

## Fixtures

Fixtures representing myEnergy responses should contain only the minimum fields needed by the test.

Prefer obviously synthetic identifiers, for example:

```text
meter-001
account-example
channel-import
```

Use realistic timestamp and energy formats where those details affect behaviour.

Do not retain unrelated fields from real customer responses.

## Dependencies

Keep dependencies minimal.

Before adding a package, check whether the Python standard library or an existing project dependency is adequate.

A dependency is justified when it materially simplifies or improves correctness of the implementation.

Avoid dependencies solely to save a few lines of straightforward code.

## Error handling

Distinguish meaningful failure modes where callers need to react differently, particularly:

- authentication expired;
- authentication challenge required;
- transport failure;
- upstream response changed or invalid;
- requested data unavailable.

Do not silently swallow unexpected upstream changes.

Error messages should aid debugging without including sensitive request or response content.

## Time and energy data

Be explicit about:

- timezone;
- UTC versus local timestamps;
- interval start/end semantics;
- interval duration;
- units;
- cumulative versus interval energy.

Do not use naive datetimes for meter data.

Victoria's daylight-saving transitions must be handled intentionally and covered by focused tests.

## Comments and documentation

Keep documentation, PR descriptions, and handoffs concise and proportional to the change. State the essential contract, non-obvious decisions, and material limitations. Use only the examples needed to make them clear.

Avoid repeating code, existing guidance, or the same rule in multiple sections. Link to existing detail instead. Omit speculative future work, exhaustive caveats, and lists of things the change does not do unless needed to prevent a likely misunderstanding.

Comment decisions and non-obvious protocol behaviour, not obvious Python syntax.

When reverse engineering undocumented behaviour, document assumptions close to the relevant code.

If behaviour is observed rather than guaranteed by published documentation, say so.

## Scope discipline

Do not implement these unless an issue specifically calls for them:

- HAN/local meter access;
- real-time power monitoring;
- tariff engines;
- unrelated distributors;
- Gmail/OTP automation;
- generic plugin systems;
- unnecessary compatibility abstractions.

The project can grow when real requirements justify it.

## Before finishing a change

Check that:

- the current issue is actually addressed;
- sensitive data has not been added;
- architecture boundaries remain intact;
- tests are proportionate and pass;
- documentation is updated if behaviour or assumptions changed;
- no unrelated refactoring has been bundled into the change;
- the actual diff and generated artifacts were checked for secrets, identifiers, and real readings;
- the handoff states what was verified and what remains uncertain.

Prefer a small, complete change over a broad speculative one.
