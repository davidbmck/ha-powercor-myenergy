# myEnergy authentication and session probe

Observed across user-led logins and manual session trials. These are observations of an undocumented portal, not a guaranteed protocol or session lifetime. No credential, OTP, cookie value, account identifier, request body, or meter reading is recorded here.

## Observed sequence

1. Reloading an Energy usage tab from the previous session led to `/s/login/`, even though browser cookies with names such as `sid` were still present. The redirect to the login page is a useful reauthentication signal; cookie presence alone is not proof of a live session.
2. The login form submitted one Salesforce Aura action: `LightningLoginFormController.communityLogin` with `username`, `password`, and `startUrl` parameters.
3. The browser visited `/secur/frontdoor.jsp` and then `/apex/MFA_Enabler`. The challenge used `/apexremote` calls to `MFAEnablerController.initiateMFA` and `MFAEnablerController.verifyMFA`. The user supplied the OTP. The browser then posted `/MFA_Enabler` and returned to `/s/energy-usage`.
4. The Energy usage page made `EnergyInformationController.getMeterData` Aura requests. A captured `Customer Access NEM12` request replayed from the authenticated browser returned HTTP 200, action state `SUCCESS`, and a nonempty `nmiData` value. The replay discarded the actual readings.

The login response set `oinfo`; the frontdoor response set `autocomplete`, `lloopch_loid`, `lloopch_lpid`, `sid`, `sid_Client`, `clientSrc`, `inst`, `oid`, and `__Secure-has-sid`; the Energy usage response set `renderCtx` and one `__Host-ERIC_...` cookie. These are observed names, **not** a tested minimum set. On the Energy usage URL, `sid`, `sid_Client`, `clientSrc`, `inst`, `renderCtx`, and `__Secure-has-sid` were browser-session cookies. `oinfo`, `autocomplete`, and `oid` had persistent expiry dates. The browser's consent and analytics cookies are not assumed to be authentication requirements.

## What the local probe established

[`tools/check_session.py`](../tools/check_session.py) loaded a private export of the freshly authenticated browser cookies and fetched `/s/energy-usage`. The original `session_accepted` label was misleading: a later control check received the same page with **no cookies**. Page-only checks now report `page_reachable_auth_unverified`; they do not establish authentication or session survival. The probe prints only a status and discards the page body.

The optional `--request-file` mode replays one captured NEM12 Aura request and reports whether meter data was returned, without printing or saving it. It reported `meter_data_retrieved` in two separate Python processes using the same cookie and request exports. This verifies unattended retrieval over a short process restart; it does not establish the session's idle lifetime. A request template contains an Aura token and customer identifier, so it must remain outside the repository in a private file. Both private exports were deleted after the check.

### Probe inputs

The required cookie file is a JSON array of browser cookies for `myenergy.powercor.com.au`, with `name`, `value`, `domain`, `path`, `secure`, and `expires` fields. Use `null` for a browser-session cookie's `expires`. The optional request file is a JSON object with the captured Aura `url` and form-encoded `body`. Both files must be absolute paths outside this repository and readable only by their owner (`chmod 600`). Never paste their contents into a terminal transcript, issue, or PR.

Cookies are loaded once at the start of a check or trial. Both timed modes and retries share the same cookie jar, so `Set-Cookie` updates and deletions from page and API responses are applied to subsequent requests. The original export is not modified. The earlier two doubling-wait runs reloaded the original export at every check and did not retain cookie changes between checks; their results therefore do not establish the lifetime of a client that adopts refreshed cookies.

Optionally pass `--cookie-state-file /absolute/private/current-cookies.json` to save the current jar after each check (including its retry or a failed check). Snapshots are written atomically with owner-only permissions, outside the repository, and must be distinct from the input export. They include browser-session cookies and preserve cookie scope. To resume later, supply a saved snapshot as the positional cookie input and choose a different snapshot output and trial log. This starts a new timing run; it does not continue the old schedule. Without this option, updated cookies are retained only in memory. By default the captured Aura request context is reused unchanged. Pass `--refresh-context` with `--request-file` to replace it before every meter request, including retries.

```text
python tools/check_session.py /absolute/private/session.cookies.json
python tools/check_session.py /absolute/private/session.cookies.json --request-file /absolute/private/nem12-request.json
```

The probe deliberately sends requests only to `https://myenergy.powercor.com.au`. Exit code `0` means the requested check succeeded (page-only success does not verify authentication); `1` means login is required; `2` means access denied, an unexpected response, or transport/configuration failure. It does not perform login or OTP entry.

The probe retries once after 10 seconds for a transport error, HTTP 429 or 5xx, or an unclassified Aura action error. It does not retry a login redirect, explicit Apex class access denial (`meter_access_denied`), HTTP 4xx other than 429, or an unexpected success-response shape. A one-shot check prints the first and final outcomes when it retries; a timed trial records them as `initial_result` and `result` in the same log event. A successful retry lets the timed trial continue. Access denial and other Aura action errors remain distinct from proven reauthentication: their error messages are neither printed nor saved.

### Refreshing API context

`--refresh-context` works for one-shot checks and both timed modes. Each check fetches the Energy usage page, reads its JSON `auraConfig`, and replaces `aura.context`, `aura.token`, and `aura.pageURI` in memory. The meter action and private request template on disk remain unchanged. When the page references an external `inline.js`, the probe fetches that same-origin script to obtain the configuration. It parses JSON without executing JavaScript.

The observed bootstrap names a temporary token cookie through `eikoocnekot`. The probe consumes that cookie and removes it from the jar, matching the browser's behaviour. Missing configuration or token reports `api_context_unavailable`; it does not fall back to stale context. An Aura invalid-session response reports `api_context_invalid`. HTTP redirects and the observed HTTP-200 JavaScript login redirect report `reauthentication_required`.

```text
python tools/check_session.py /absolute/private/session.cookies.json \
  --request-file /absolute/private/nem12-request.json --refresh-context \
  --cookie-state-file /absolute/private/refreshed-cookies.json
```

The direct-client path observed on 8 October requires three requests per check (page, inline bootstrap, meter API), versus two without context refresh. At 80-minute intervals this is 54 scheduled requests per day instead of 36, excluding retries and redirects. Refreshing API context still requires a valid authenticated session; it cannot replace login or OTP after that session expires.

### Explicit session renewal

Pass `--renew-session` with `--request-file` to run the observed Continue working sequence before each meter check, in one-shot, fixed-interval, or doubling-wait mode. This option implies `--refresh-context`; it does not change the selected cadence or add background keep-alive traffic between checks. Existing behaviour remains the default when the option is omitted.

Each attempt obtains fresh API context, reads the session timer, calls `SessionTimeoutWarnController.getSessionRefreshAction`, reads the timer with `updateTimedOutSession=true`, and reads it again independently before fetching meter data. Only a single SUCCESS action with boolean true is accepted as renewal acknowledgement. A nonpositive remaining session time or a login redirect stops with `reauthentication_required`. Rejected actions and malformed responses stop with `session_renewal_rejected` or `unexpected_renewal_response`; no meter request follows a failed renewal. Renewal transport failures and HTTP 429/5xx receive the existing single retry, including fresh context and a new renewal sequence.

One-shot output and trial events include `renewal_attempts`. Each entry contains only the result and available `before`, `update`, and `after` numeric `warning_seconds`/`remaining_seconds` readings. A retry retains both attempts. The probe accepts a positive timer without requiring an increase, since a newly active session can already be at its maximum. Successful renewal is not counted as successful meter retrieval until the meter request itself succeeds. Cookie updates from all steps share the same jar and optional private snapshot; original inputs are unchanged.

```text
python tools/check_session.py /absolute/private/session.cookies.json \
  --request-file /absolute/private/nem12-request.json --renew-session \
  --cookie-state-file /absolute/private/renewal-current.json \
  --trial active --hours 168 --every-minutes 80 \
  --log-file /absolute/private/renewal-7d.jsonl
```

On the observed external-bootstrap path this makes seven requests per check: page, bootstrap, timer before, renewal action, timer update, timer after, and meter data. That is approximately 126 requests per day at 80-minute intervals, excluding retries, redirects and the trial's initial/final boundary count. The three timer reads intentionally provide evidence about renewal; reducing diagnostic traffic can be considered after validation.

### Measuring session lifetime

Start each trial promptly after a fresh, user-led login and a private cookie/request export. Use a **different session and a different log file for each trial**. The tool measures elapsed time from process start, so record any delay between login and starting it. Keep the computer and process running; the UTC timestamps in the log show when probes actually happened if the computer sleeps.

A doubling-wait trial starts with five minutes of no requests, then checks the Energy usage page and NEM12 request. After each success it waits 10, 20, 40 minutes, and so on before the next check. It stops at the first failed probe or when interrupted (for example with Ctrl-C). `--initial-minutes` changes the first wait:

```text
python tools/check_session.py /absolute/private/idle.cookies.json \
  --request-file /absolute/private/idle-request.json \
  --cookie-state-file /absolute/private/idle-current.json \
  --trial idle --initial-minutes 5 --log-file /absolute/private/doubling.jsonl
```

An active trial checks immediately and then at fixed intervals, including the end of the trial. Each check fetches the Energy usage page and replays the NEM12 request:

```text
python tools/check_session.py /absolute/private/active.cookies.json \
  --request-file /absolute/private/active-request.json \
  --cookie-state-file /absolute/private/active-current.json \
  --trial active --hours 24 --every-minutes 30 \
  --log-file /absolute/private/active-24h.jsonl
```

The JSONL log contains only UTC time, elapsed seconds, planned wait, trial settings, outcomes, and retry timing. It is created with owner-only permissions outside the repository. `reauthentication_required` is distinct from meter action, unexpected response, and transport errors; investigate the latter before treating them as expiry. The active trial tests the combined effect of page and meter requests, not which individual request renews the session.

The doubling-wait run measures **survival under spaced requests**. Every successful check may renew the session, so its final elapsed time is not a strict idle timeout. Establishing the untouched idle timeout still requires fresh sessions with just one check after each chosen wait. Compare those results with this run and an active trial before concluding whether requests extend lifetime.

## Observed results and limits

Two doubling-wait trials retrieved data after gaps through 80 minutes, then
failed after a 160-minute gap. A later protected request was denied and a
browser reload required login. This suggests an inactivity-related limit,
but does not establish the exact timeout or separate session expiry from
stale API context. The earlier probes also reloaded the original cookies on
each check, so cookie handling is a confounder.

Short checks established adoption of server cookie updates and reuse of a
private snapshot in a fresh process. They did not demonstrate rotation of the
main authentication cookie or a longer session lifetime. A subsequent 24-hour
trial at 30-minute intervals completed without retries or failures.

Two trials at 80-minute intervals stopped after roughly two to three days,
one with refreshed API context and one without it. Later checks required
reauthentication. The first failure's original exception was not retained;
these observations do not prove a fixed absolute session limit.

The browser's `salesforceIdentity:sessionTimeoutWarn` component uses
`SessionTimeoutWarnController.getSessionRefreshAction`, then the session
servlet with `updateTimedOutSession=true`, for **Continue working**. Its
`sp` value controls the warning timing; `sr` is the remaining session time.
A manual check of this sequence reset the timer to approximately two hours.
The browser can also initiate logout, so a portal tab sharing the same
session can confound a standalone trial.

A completed seven-day trial at 80-minute intervals combined explicit renewal,
fresh Aura context and cookie adoption, with the shared browser tab closed
without logging out. All 127 meter checks and renewals succeeded, with no
retries or failures. This establishes seven days of observed access, not an
indefinite lifetime. The individual contributions of those measures were not
isolated; near-full timers after bootstrap do not prove which request renewed
access. The standalone client subsequently validated a shorter sequence that
omits the diagnostic pre-renewal timer read; see the [client contract](client.md).

The observed portal omitted its public DigiCert intermediate certificate.
It was verified against the system root store before being supplied with
`--intermediate-ca`. See [certificate provenance](../NOTICE); never disable
TLS or hostname verification.

The minimum necessary cookie subset, untouched idle lifetime, absolute
session lifetime and long-term recovery reliability remain unverified. Apex
access denial alone is not proof of expiry. The [HA integration](home-assistant.md)
provides manual reauthentication and optional automatic IMAP verification;
the development probe itself performs neither login nor OTP entry.
