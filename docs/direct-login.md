# Direct login protocol

The proven sequence now lives in `myenergy`; `tools.probe_login` is a thin
client caller. See the [client contract](client.md) and
[Home Assistant setup and recovery](home-assistant.md).

## Observed browser sequence

Observed with user-entered credentials and email OTP:

1. Fetch `/s/login/` and its same-origin `inline.js`. Parse `auraConfig` as
   JSON. Guest context identifies `siteforce:loginApp2`; the login request
   uses the literal form token `null`.
2. POST `/s/sfsites/aura` with one `aura://ApexActionController/ACTION$execute`
   action for `LightningLoginFormController.communityLogin`. Its parameters
   in this observation were `username` and `password`. The browser appends
   `.VPN` to the entered email for this portal; it passes the password unchanged.
   This transformation is visible in `c:communityLoginLwc`, with the brand
   mapping in `c/utilities`. Form fields are
   `message`, `aura.context`, `aura.pageURI`, and `aura.token`.
3. Read `actions[0].returnValue.returnValue.data.loginUrl` only after
   `isSuccess: true`. Follow its `/secur/frontdoor.jsp` navigation and redirects to
   `/apex/MFA_Enabler`, retaining cookies throughout. Never log that URL:
   its query carries session material.
   The observed frontdoor returns HTTP 200 with `lhdoredir()` calling
   `window.location.replace(...)`; parse only that JSON string literal and
   require the same-origin MFA path rather than executing the page script.
4. Parse the JSON argument of `new $VFRM.RemotingProviderImpl(...)` on the
   MFA page. It supplies the `/apexremote` service, page context and per-method
   CSRF/authorization fields. These are session secrets, not constants.
5. Call `MFAEnablerController.initiateMFA` with `data: ["email"]`.
   Its successful result contains `success`, `verificationIdentifier`, and
   `isRegistration`. Retain the identifier for this challenge only.
6. Call `verifyMFA` with four ordered arguments: six-digit code, verification
   identifier, registration boolean, and `"email"`.
7. After verification succeeds, mirror `redirectToHome()`: POST its generated
   form/action fields to `/MFA_Enabler`, including the Visualforce
   `ViewState`, `ViewStateVersion`, `ViewStateMAC`, and `ViewStateCSRF` fields.
   Follow the landing redirect to `/s/`.
8. Obtain fresh authenticated Aura context using the session probe's page
   bootstrap mechanism. Authenticated context consumes the temporary token
   cookie named by `eikoocnekot`; do not continue with the guest token.

Remoting requests are JSON objects with `action`, `method`, `data`, `type:
"rpc"`, `tid`, and `ctx`. Context keys observed were `csrf`, `vid`, `ns`,
`ver`, and `authorization`. Responses are arrays; correlate the response
method and transaction ID before accepting `result.success`.

The page loads reCAPTCHA, but the observed login action has no CAPTCHA argument;
whether other accounts require a browser challenge remains unverified.

## Account discovery

The dashboard calls `DashboardController.getEnergyUsageData` through the same
Apex action wrapper, with `nmiAssignId: null`, `noOfLimit: 50`, and
`isDashboard: true`. The useful response path is
`actions[0].returnValue.returnValue.data.nmiAssignmentsForEnergyUsage`.
Assignment records include `Id`, `NMI__c`, and `NMI__r.Name`. The response also
reports `totalCountNmiAssignsForEnergyUsage`; the client must not silently truncate
accounts with more results than the requested limit.

Retain only access fields: the response includes unrelated personal data, and
assignment IDs and NMI values remain sensitive. The meter request's `company`
comes from `EnergyInformationController.retrieveAccountHolderInfo(nmi)`
`data.companyName`, observed as `Powercor` in the live check.

## Interactive HTTP check

Install `requirements.txt` in a virtual environment, then run from the project
root:

```text
python -m tools.probe_login
```

The probe starts with an empty cookie jar and prompts for email/password and OTP
with echo disabled. Success requires protected
meter discovery. The probe neither retries nor saves sessions, prints only safe
outcomes/counts, and does not handle MFA registration.

`--credentials-file /absolute/private/login.json` reads an existing owner-only
(`chmod 600`) JSON file outside the repository with `username` and `password`.
Use the ordinary email; the probe adds the portal suffix. Edit credentials
locally, never in command arguments or chat. The file is a development input,
not a production credential store, and is not modified by the probe.

`--bootstrap-only` checks public context discovery without credentials.
`--intermediate-ca /absolute/private/intermediate.pem` adds a verified public
intermediate to the normal TLS trust store if the server omits its chain;
certificate and hostname verification remain enabled.

### Automatic IMAP code retrieval

Add `--imap-config /absolute/private/imap.json` to retrieve the code without
terminal prompts when `--credentials-file` is also supplied. The owner-only
JSON file must be outside the repository and contain `host`, `username`,
`password`, `folder`, `sender`, `recipient`, and `subject`. For the dedicated
Gmail inbox, use `imap.gmail.com`, `INBOX`, and a Google app password without
display spaces. Set `subject` to `{code} is your one-time verification code.`;
the addresses must match the sender and recipient headers exactly.

Verified TLS on port 993 and read-only folder selection establish UIDVALIDITY
and UIDNEXT before MFA. Polling considers only new UIDs with matching addresses
and subject, fetching From/To/Subject/Date headers with `BODY.PEEK`. Bodies and
flags remain untouched; codes are never printed or saved.

The exact subject template must yield one six-digit code. Its Date may precede
the request by at most five seconds to allow observed one-second clock skew;
the UID baseline excludes existing mail. Conflicting codes or changed
UIDVALIDITY stop the run. Polling lasts two minutes, with a 15-second timeout
per network operation. Failure stops before verification; omit `--imap-config`
to use manual entry.

Use one login attempt at a time, with no concurrent browser login or resend.
The observed email does not expose the portal's verification identifier, so
UID/date freshness and exact header matching associate it with the single
pending challenge; they cannot disambiguate concurrent challenges. Process
restart starts a new login and a new mailbox baseline.

The [Home Assistant IMAP integration](https://www.home-assistant.io/integrations/imap/)
was considered first, but its content events can be recorded. The standalone
client instead uses a standard-library helper in a worker thread, keeping headers
and codes transient.

## Validation and remaining handoff

- Manual HTTP login and consecutive independent automatic IMAP logins passed.
  All reached protected discovery
  without browser exports, with TLS verification enabled. No raw capture was saved.
- Synthetic tests cover authentication parsing, IMAP freshness/ambiguity,
  cleanup and stopping on failure, including the observed clock-skew timeout.
- The client passed fresh automatic login and protected discovery, session
  reload in a fresh process and normalized interval retrieval.
- An earlier unexpected MFA response remains unclassified and was not reproduced.

These checks demonstrate the direct and automatic email-MFA paths for one
existing account, not long-term reliability or every account/challenge type.

The standalone client consolidates the shared protocol, refreshes context after session
reload, and retrieves normalized intervals. Invalid/expired OTP classification
still needs evidence. See the [session investigation](authentication-session.md)
for the completed trial and its reliability limits.
