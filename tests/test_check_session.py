from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from http.cookiejar import CookieJar
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode
from urllib.request import HTTPSHandler
from urllib.response import addinfourl

from tools.check_session import (
    load_cookies, load_request, meter_response_outcome, probe_session, probe_with_retry, retryable_outcome,
    run_trial, write_event, renew_session, SESSION_REFRESH_ACTION, main,
)


class SessionProbeTests(unittest.TestCase):
    def test_command_errors_do_not_expose_private_paths_or_exception_values(self):
        for error in (FileNotFoundError(2, "not found", "/absolute/private/synthetic-session.json"),
                      ValueError("synthetic-private-response")):
            output = io.StringIO()
            with (self.subTest(error=type(error).__name__),
                  patch("sys.argv", ["check_session", "/absolute/private/cookies.json"]),
                  patch("tools.check_session.check_session", side_effect=error),
                  redirect_stderr(output)):
                self.assertEqual(main(), 2)
            self.assertEqual(output.getvalue(), "probe_error: input, TLS or response validation failed\n")

    @staticmethod
    def private_inputs(directory: str) -> tuple[Path, Path, Path]:
        root = Path(directory)
        cookie = root / "cookies.json"
        request = root / "request.json"
        log = root / "trial.jsonl"
        cookie.write_text("[]")
        action = {"actions": [{"params": {
            "classname": "EnergyInformationController",
            "method": "getMeterData",
            "params": {"formatType": "Customer Access NEM12"},
        }}]}
        request.write_text(json.dumps({
            "url": "https://myenergy.powercor.com.au/s/sfsites/aura",
            "body": urlencode({"message": json.dumps(action)}),
        }))
        os.chmod(cookie, 0o600)
        os.chmod(request, 0o600)
        return cookie, request, log

    def test_meter_data_success_shape(self):
        response = {
            "actions": [{
                "state": "SUCCESS",
                "returnValue": {"returnValue": {
                    "isSuccess": True,
                    "data": {"body": {"status": "Success", "nmiData": "100,NEM12,synthetic"}},
                }},
            }],
        }
        self.assertEqual(meter_response_outcome(json.dumps(response).encode()), "meter_data_retrieved")
        response["actions"][0]["state"] = "ERROR"
        self.assertEqual(meter_response_outcome(json.dumps(response).encode()), "meter_action_error")

    def test_retry_only_transient_outcomes(self):
        self.assertTrue(retryable_outcome("meter_action_error"))
        self.assertTrue(retryable_outcome("meter_http_error:503"))
        self.assertTrue(retryable_outcome("upstream_http_error:429"))
        self.assertFalse(retryable_outcome("meter_http_error:403"))
        self.assertFalse(retryable_outcome("unexpected_meter_response"))

    def test_framed_aura_context_error_is_classified(self):
        body = b'*/{"event":{"descriptor":"markup://aura:invalidSession","attributes":{"values":{"newToken":"invalid_csrf"}}}}/*ERROR*/'
        self.assertEqual(meter_response_outcome(body), "api_context_invalid")
        self.assertEqual(meter_response_outcome(b"<html>unexpected</html>"), "unexpected_meter_response")

    def test_script_login_redirect_stops_before_meter_request(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie, request, _ = self.private_inputs(directory)
            response = MagicMock()
            response.__enter__.return_value = response
            response.geturl.return_value = "https://myenergy.powercor.com.au/s/energy-usage"
            response.status = 200
            response.read.return_value = b"<script>function redirectOnLoad(){window.location.replace('/s/login/');}redirectOnLoad();</script>"
            with patch("tools.check_session.build_opener") as opener:
                opener.return_value.open.return_value = response
                self.assertEqual(probe_session(cookie, request_file=request), (1, "reauthentication_required"))
            opener.return_value.open.assert_called_once()

    def test_access_denied_is_distinct_and_not_retried(self):
        response = {"actions": [{"state": "ERROR", "error": [{
            "message": "You do not have access to the Apex class named 'ExampleController'.",
        }]}]}
        outcome = meter_response_outcome(json.dumps(response).encode())
        self.assertEqual(outcome, "meter_access_denied")
        with (
            patch("tools.check_session.load_cookies", return_value=CookieJar()),
            patch("tools.check_session.probe_session", return_value=(2, outcome)) as probe,
            patch("tools.check_session.sleep_until") as sleep,
        ):
            self.assertEqual(probe_with_retry(Path("/unused"), None, None), (2, outcome, None))
        probe.assert_called_once()
        sleep.assert_not_called()

    def test_anonymous_page_success_does_not_establish_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie, _, _ = self.private_inputs(directory)
            response = MagicMock()
            response.__enter__.return_value = response
            response.geturl.return_value = "https://myenergy.powercor.com.au/s/energy-usage"
            response.status = 200
            response.read.return_value = b"<html>Public page shell</html>"
            with patch("tools.check_session.build_opener") as opener:
                opener.return_value.open.return_value = response
                self.assertEqual(probe_session(cookie), (0, "page_reachable_auth_unverified"))

    def test_action_error_retries_once_and_preserves_initial_result(self):
        with (
            patch("tools.check_session.load_cookies", return_value=CookieJar()),
            patch("tools.check_session.probe_session", side_effect=[
                (2, "meter_action_error"), (0, "meter_data_retrieved"),
            ]) as probe,
            patch("tools.check_session.time.monotonic", return_value=100),
            patch("tools.check_session.sleep_until") as sleep,
        ):
            result = probe_with_retry(Path("/unused"), None, None)
        self.assertEqual(result, (0, "meter_data_retrieved", "meter_action_error"))
        self.assertEqual(probe.call_count, 2)
        sleep.assert_called_once_with(110)

    def test_only_expected_meter_action_can_be_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path, _ = self.private_inputs(directory)
            self.assertEqual(load_request(path).get_method(), "POST")
            payload = json.loads(path.read_text())
            payload["url"] = "https://example.invalid/s/sfsites/aura"
            path.write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                load_request(path)

    def test_idle_trial_doubles_wait_until_reauthentication(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie, request, log = self.private_inputs(directory)
            clock = [0.0]
            deadlines = []

            def fake_sleep(deadline):
                if not deadlines:
                    self.assertEqual(probe.call_count, 0)
                deadlines.append(deadline)
                clock[0] = deadline

            results = [(0, "meter_data_retrieved")] * 3 + [(1, "reauthentication_required")]
            with (
                patch("tools.check_session.time.monotonic", side_effect=lambda: clock[0]),
                patch("tools.check_session.sleep_until", side_effect=fake_sleep),
                patch("tools.check_session.probe_session", side_effect=results) as probe,
            ):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(run_trial("idle", None, None, cookie, None, request, log), 1)
            self.assertEqual(probe.call_count, 4)
            self.assertEqual(deadlines, [300, 900, 2100, 4500])
            events = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([event["wait_seconds"] for event in events[1:]], [300, 600, 1200, 2400])
            self.assertEqual(events[-1]["result"], "reauthentication_required")
            self.assertEqual(log.stat().st_mode & 0o077, 0)

    def test_idle_trial_records_retry_then_stops_on_repeated_action_error(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie, request, log = self.private_inputs(directory)
            clock = [0.0]
            with (
                patch("tools.check_session.time.monotonic", side_effect=lambda: clock[0]),
                patch("tools.check_session.sleep_until", side_effect=lambda deadline: clock.__setitem__(0, deadline)),
                patch("tools.check_session.probe_session", return_value=(2, "meter_action_error")) as probe,
            ):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(run_trial("idle", None, None, cookie, None, request, log), 2)
            self.assertEqual(probe.call_count, 2)
            event = json.loads(log.read_text().splitlines()[-1])
            self.assertEqual(event["result"], "meter_action_error")
            self.assertEqual(event["initial_result"], "meter_action_error")
            self.assertEqual(event["retry_wait_seconds"], 10)
            self.assertEqual(event["elapsed_seconds"], 310)

    def test_active_trial_checks_at_intervals_and_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie, request, log = self.private_inputs(directory)
            clock = [0.0]

            def fake_probe(*_args, **_kwargs):
                clock[0] += 0.1
                return 0, "meter_data_retrieved"

            with (
                patch("tools.check_session.time.monotonic", side_effect=lambda: clock[0]),
                patch("tools.check_session.sleep_until", side_effect=lambda deadline: clock.__setitem__(0, deadline)),
                patch("tools.check_session.probe_session", side_effect=fake_probe) as probe,
            ):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(run_trial("active", 3 / 3600, 1 / 60, cookie, None, request, log), 0)
            self.assertEqual(probe.call_count, 4)
            events = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([event["elapsed_seconds"] for event in events[1:]], [0.1, 1.1, 2.1, 3.1])

    def test_cookie_rotation_and_deletion_survive_retries_and_trial_checks(self):
        success = json.dumps({"actions": [{"state": "SUCCESS", "returnValue": {
            "returnValue": {"isSuccess": True, "data": {"body": {
                "status": "Success", "nmiData": "100,NEM12,synthetic",
            }}},
        }}]}).encode()
        error = b'{"actions":[{"state":"ERROR"}]}'
        for trial in ("idle", "active"):
            with self.subTest(trial=trial), tempfile.TemporaryDirectory() as directory:
                cookie, request, log = self.private_inputs(directory)
                cookie.write_text(json.dumps([{
                    "name": "sid", "value": "synthetic-initial", "domain": "myenergy.powercor.com.au",
                    "path": "/", "secure": True, "expires": None,
                }]))
                original_export = cookie.read_bytes()
                state = Path(directory) / "updated-cookies.json"
                # Exercise urllib's real CookieProcessor using synthetic HTTPS responses.
                exchanges = iter([
                    ("sid=synthetic-initial", "sid=synthetic-page; Path=/; Secure", b"page"),
                    ("sid=synthetic-page", "sid=synthetic-error; Path=/; Secure", error),
                    ("sid=synthetic-error", "sid=synthetic-retry; Path=/; Secure", b"page"),
                    ("sid=synthetic-retry", "sid=synthetic-next; Path=/; Secure", success),
                    ("sid=synthetic-next", None, b"page"),
                    ("sid=synthetic-next", "sid=; Max-Age=0; Path=/; Secure", success),
                    (None, None, b"loginApp2"),
                ])
                test = self
                observed = []

                class SyntheticHTTPS(HTTPSHandler):
                    def https_open(self, req):
                        expected, replacement, body = next(exchanges)
                        test.assertEqual(req.get_header("Cookie"), expected)
                        if len(observed) == 4:
                            restored = list(load_cookies(state))
                            test.assertEqual([(c.name, c.value) for c in restored], [("sid", "synthetic-next")])
                            test.assertFalse(restored[0].domain_specified)
                            test.assertTrue(restored[0].secure)
                            test.assertIsNone(restored[0].expires)
                        observed.append(req.get_header("Cookie"))
                        headers = Message()
                        if replacement:
                            headers["Set-Cookie"] = replacement
                        response = addinfourl(io.BytesIO(body), headers, req.full_url, 200)
                        response.msg = "OK"
                        return response

                clock = [0.0]
                with (
                    patch("tools.check_session.HTTPSHandler", return_value=SyntheticHTTPS()),
                    patch("tools.check_session.time.monotonic", side_effect=lambda: clock[0]),
                    patch("tools.check_session.sleep_until", side_effect=lambda deadline: clock.__setitem__(0, max(clock[0], deadline))),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    result = run_trial(
                        trial, 40 / 3600 if trial == "active" else None,
                        20 / 60 if trial == "active" else None,
                        cookie, None, request, log, initial_minutes=1 / 60,
                        cookie_state_file=state,
                    )
                self.assertEqual(result, 1)
                self.assertEqual(len(observed), 7)
                self.assertEqual(json.loads(state.read_text()), [])
                self.assertEqual(state.stat().st_mode & 0o777, 0o600)
                self.assertEqual(cookie.read_bytes(), original_export)
                self.assertNotIn("synthetic-", output.getvalue())

    def test_cookie_state_cannot_overwrite_inputs_or_log(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie, request, log = self.private_inputs(directory)
            for state in (cookie, request, log):
                with self.subTest(state=state.name), self.assertRaisesRegex(ValueError, "overwrite"):
                    run_trial("idle", None, None, cookie, None, request, log, cookie_state_file=state)

    def test_context_refresh_on_every_check_and_retry(self):
        for external in (False, True):
            with self.subTest(external_script=external), tempfile.TemporaryDirectory() as directory:
                cookie, request, log = self.private_inputs(directory)
                original = request.read_bytes()
                page_count = [0]
                sent_tokens = []
                test = self

                def config_body():
                    return ("var auraConfig = " + json.dumps({
                        "eikoocnekot": "__Host-synthetic-token",
                        "context": {"mode": "PROD", "app": "example:app", "loaded": {},
                                    "fwuid": f"synthetic-fwuid-{page_count[0]}", "uad": 1},
                        "ignored": "semicolon; inside a string",
                    }) + ";").encode()

                class SyntheticHTTPS(HTTPSHandler):
                    def https_open(self, req):
                        headers = Message()
                        if req.data is None:
                            if req.full_url.endswith("/s/energy-usage"):
                                page_count[0] += 1
                                body = b'<script src="/s/sfsites/l/synthetic/inline.js"></script>' if external else config_body()
                            else:
                                test.assertEqual(req.full_url, "https://myenergy.powercor.com.au/s/sfsites/l/synthetic/inline.js")
                                body = config_body()
                            if not external or req.full_url.endswith("/inline.js"):
                                headers["Set-Cookie"] = f"__Host-synthetic-token=synthetic-token-{page_count[0]}; Path=/; Secure"
                        else:
                            form = parse_qs(req.data.decode())
                            token = form["aura.token"][0]
                            sent_tokens.append(token)
                            test.assertEqual(token, f"synthetic-token-{page_count[0]}")
                            context = json.loads(form["aura.context"][0])
                            test.assertEqual(context["fwuid"], f"synthetic-fwuid-{page_count[0]}")
                            test.assertIs(context["uad"], True)
                            test.assertEqual(form["message"], parse_qs(json.loads(original)["body"])["message"])
                            test.assertNotIn("__Host-synthetic-token", req.get_header("Cookie", ""))
                            body = b'{"actions":[{"state":"ERROR"}]}' if len(sent_tokens) == 1 else json.dumps({"actions": [{
                                "state": "SUCCESS", "returnValue": {"returnValue": {
                                    "isSuccess": True, "data": {"body": {"status": "Success", "nmiData": "synthetic"}},
                                }},
                            }]}).encode()
                        response = addinfourl(io.BytesIO(body), headers, req.full_url, 200)
                        response.msg = "OK"
                        return response

                clock = [0.0]
                with (
                    patch("tools.check_session.HTTPSHandler", return_value=SyntheticHTTPS()),
                    patch("tools.check_session.time.monotonic", side_effect=lambda: clock[0]),
                    patch("tools.check_session.sleep_until", side_effect=lambda deadline: clock.__setitem__(0, max(clock[0], deadline))),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    result = run_trial("active", 40 / 3600, 20 / 60, cookie, None, request, log, refresh_context=True)
                self.assertEqual(result, 0)
                self.assertEqual(sent_tokens, [f"synthetic-token-{n}" for n in range(1, 5)])
                self.assertEqual(request.read_bytes(), original)
                self.assertTrue(json.loads(log.read_text().splitlines()[0])["refresh_context"])
                self.assertNotIn("synthetic-token", output.getvalue())

    def test_refresh_fails_closed_for_missing_or_untrusted_context(self):
        for body in (b"<html>no context</html>", b'<script src="https://example.invalid/s/sfsites/l/example/inline.js"></script>',
                     b'var auraConfig = {"eikoocnekot":"missing-cookie","context":{"mode":"PROD","fwuid":"synthetic","app":"example:app","loaded":{},"uad":1}};'):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                cookie, request, _ = self.private_inputs(directory)
                response = MagicMock()
                response.__enter__.return_value = response
                response.geturl.return_value = "https://myenergy.powercor.com.au/s/energy-usage"
                response.status = 200
                response.read.return_value = body
                with patch("tools.check_session.build_opener") as opener:
                    opener.return_value.open.return_value = response
                    self.assertEqual(probe_session(cookie, request_file=request, refresh_context=True), (2, "api_context_unavailable"))
                opener.return_value.open.assert_called_once()

    def test_renewal_sequence_across_retry_and_timed_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie, request, log = self.private_inputs(directory)
            original = request.read_bytes()
            observed = []
            test = self
            page_count = [0]
            stage = [0]

            class SyntheticHTTPS(HTTPSHandler):
                def https_open(self, req):
                    headers = Message()
                    if req.full_url.endswith('/s/energy-usage'):
                        page_count[0] += 1
                        stage[0] = 0
                        observed.append('page')
                        body = ('var auraConfig = ' + json.dumps({
                            'eikoocnekot': 'temporary', 'context': {'mode': 'PROD',
                            'fwuid': 'synthetic', 'app': 'example:app', 'loaded': {}, 'uad': 1},
                        }) + ';').encode()
                        headers['Set-Cookie'] = f'temporary=synthetic-{page_count[0]}; Path=/; Secure'
                    elif 'SessionTimeServlet?' in req.full_url:
                        stage[0] += 1
                        observed.append('timer' + str(stage[0]))
                        test.assertEqual('updateTimedOutSession=true' in req.full_url, stage[0] == 2)
                        if stage[0] > 1:
                            test.assertIn('sid=renewed', req.get_header('Cookie', ''))
                        body = b'while(1);\n{"sp":7170,"sr":7200}'
                    else:
                        form = parse_qs(req.data.decode())
                        test.assertEqual(form['aura.token'], [f'synthetic-{page_count[0]}'])
                        action = json.loads(form['message'][0])['actions'][0]
                        if action.get('descriptor') == SESSION_REFRESH_ACTION:
                            observed.append('renew')
                            test.assertEqual(action['params'], {})
                            test.assertEqual(stage[0], 1)
                            headers['Set-Cookie'] = 'sid=renewed; Path=/; Secure'
                            # First renewal gets a transient HTTP error, then the
                            # entire bootstrap/renewal sequence must run again.
                            if page_count[0] == 1:
                                raise HTTPError(req.full_url, 503, 'synthetic', headers, None)
                            body = b'{"actions":[{"state":"SUCCESS","returnValue":true}]}'
                        else:
                            observed.append('meter')
                            test.assertEqual(stage[0], 3)
                            test.assertEqual(form['message'], parse_qs(json.loads(original)['body'])['message'])
                            body = json.dumps({'actions': [{'state': 'SUCCESS', 'returnValue': {
                                'returnValue': {'isSuccess': True, 'data': {'body': {
                                    'status': 'Success', 'nmiData': 'synthetic'}}}}}]}).encode()
                    response = addinfourl(io.BytesIO(body), headers, req.full_url, 200)
                    response.msg = 'OK'
                    return response

            clock = [0.0]
            state = Path(directory) / 'state.json'
            with (
                patch('tools.check_session.HTTPSHandler', return_value=SyntheticHTTPS()),
                patch('tools.check_session.time.monotonic', side_effect=lambda: clock[0]),
                patch('tools.check_session.sleep_until', side_effect=lambda d: clock.__setitem__(0, max(d, clock[0]))),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(run_trial('active', 20/3600, 20/60, cookie, None, request, log,
                                           renew=True, cookie_state_file=state), 0)
            self.assertEqual(observed, ['page', 'timer1', 'renew'] +
                             ['page', 'timer1', 'renew', 'timer2', 'timer3', 'meter'] * 2)
            events = [json.loads(x) for x in log.read_text().splitlines()]
            self.assertTrue(events[0]['refresh_context'])
            self.assertTrue(events[0]['renew_session'])
            self.assertEqual(events[1]['initial_result'], 'renewal_http_error:503')
            self.assertEqual(len(events[1]['renewal_attempts']), 2)
            self.assertEqual(events[-1]['renewal_attempts'][0]['after']['remaining_seconds'], 7200)
            self.assertEqual(request.read_bytes(), original)
            self.assertNotIn('synthetic', output.getvalue())
            self.assertEqual([(c.name, c.value) for c in load_cookies(state)], [('sid', 'renewed')])

    def test_renewal_stops_on_expiry_rejection_or_invalid_response(self):
        valid_timer = b'while(1);\n{"sp":50,"sr":80}'
        for bodies, expected in [
            ([b'{"sp":-1,"sr":-1}'], (1, 'reauthentication_required')),
            ([b'{"sp":true,"sr":80}'], (2, 'unexpected_renewal_response')),
            ([valid_timer, b'{"actions":[{"state":"ERROR"}]}'], (2, 'session_renewal_rejected')),
            ([valid_timer, b'{"actions":[{"state":"SUCCESS","returnValue":false}]}'], (2, 'unexpected_renewal_response')),
            ([valid_timer, b'*/{"event":{"descriptor":"markup://aura:invalidSession"}}/*ERROR*/'], (2, 'api_context_invalid')),
            ([valid_timer, b'<script>redirectOnLoad;location.replace("/s/login/")</script>'], (1, 'reauthentication_required')),
            ([valid_timer, b'{"actions":[{"state":"SUCCESS","returnValue":true}]}',
              b'{"sp":-1,"sr":-1}'], (1, 'reauthentication_required')),
        ]:
            with self.subTest(expected=expected, bodies=bodies), tempfile.TemporaryDirectory() as directory:
                _, request, _ = self.private_inputs(directory)
                responses = []
                for body in bodies:
                    response = MagicMock()
                    response.__enter__.return_value = response
                    response.status = 200
                    response.geturl.return_value = 'https://myenergy.powercor.com.au/s/sfsites/aura'
                    response.read.return_value = body
                    responses.append(response)
                opener = MagicMock()
                opener.open.side_effect = responses
                self.assertEqual(renew_session(opener, load_request(request), {}), expected)
                self.assertEqual(opener.open.call_count, len(bodies))
                self.assertFalse(retryable_outcome(expected[1]))

    def test_trial_log_cannot_be_written_in_repository(self):
        repository_log = Path(__file__).resolve().parents[1] / "trial.jsonl"
        with self.assertRaises(ValueError):
            write_event(repository_log, {"event": "probe"})


if __name__ == "__main__":
    unittest.main()
