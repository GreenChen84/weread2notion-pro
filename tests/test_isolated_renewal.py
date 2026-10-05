import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import requests

spec = importlib.util.spec_from_file_location(
    "renewal_probe", Path(__file__).resolve().parents[1] / "tools/diagnose_weread.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
MARKER = "SENSITIVE_SIMULATED_VALUE"


class Response:
    def __init__(self, data, status=200, cookies=None):
        self.data = data
        self.status_code = status
        self.cookies = cookies or requests.cookies.RequestsCookieJar()

    def json(self):
        if self.data is ...:
            raise ValueError(MARKER)
        return self.data


class RenewalTests(unittest.TestCase):
    def execute(self, responses, cookie=None, post_error=False):
        session = requests.Session()
        captured_calls = []
        responses = list(responses)

        def next_response(method, url, **kwargs):
            captured_calls.append((method, url, kwargs))
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["timeout"], 15)
            if method == "POST" and post_error:
                raise requests.RequestException(MARKER)
            response = responses.pop(0)
            session.cookies.update(response.cookies)
            return response

        if cookie is None:
            cookie = f"wr_vid={MARKER}; wr_skey={MARKER}; wr_rt={MARKER}; wr_ql=0"
        output = io.StringIO()
        with patch("requests.Session", return_value=session), \
             patch.object(session, "get", side_effect=lambda url, **kw: next_response("GET", url, **kw)), \
             patch.object(session, "post", side_effect=lambda url, **kw: next_response("POST", url, **kw)), \
             patch.dict(os.environ, {"WEREAD_COOKIE": cookie}, clear=True), \
             contextlib.redirect_stdout(output):
            result = probe.main()
        text = output.getvalue()
        self.assertNotIn(MARKER, text)
        self.assertLessEqual(sum(method == "POST" for method, _, _ in captured_calls), 1)
        rows = [json.loads(line) for line in text.splitlines()]
        return result, rows, captured_calls, session

    def test_auth_error_renewed_once_and_shelf_recovers(self):
        fresh = requests.cookies.RequestsCookieJar()
        fresh.set("wr_skey", MARKER + "_NEW", domain=".weread.qq.com", path="/")
        valid = {"books": [], "bookProgress": [], "archive": []}
        result, rows, calls, session = self.execute([
            Response({}), Response({"errCode": -2012, "errMsg": MARKER}),
            Response({"succ": 1}, cookies=fresh), Response(valid)])
        self.assertEqual(result, 0)
        self.assertEqual([(method, url) for method, url, _ in calls], [
            ("GET", probe.BASE + "/"), ("GET", probe.SHELF),
            ("POST", probe.RENEWAL), ("GET", probe.SHELF)])
        self.assertEqual(calls[2][2]["json"], {"rq": "%2Fweb%2Fshelf%2Fsync", "ql": False})
        self.assertEqual(rows[-1]["renewal_attempts"], 1)
        self.assertEqual(probe.metadata(session)["wr_skey"]["sent_count"], 1)

    def test_successful_baseline_never_renews(self):
        result, rows, calls, _ = self.execute([
            Response({}), Response({"books": [], "bookProgress": [], "archive": []})])
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(rows[-1]["renewal_attempts"], 0)

    def test_missing_refresh_cookie_never_renews(self):
        result, rows, calls, _ = self.execute([Response({}), Response({"errCode": -2012})],
                                             cookie=f"wr_vid={MARKER}; wr_skey={MARKER}")
        self.assertEqual(result, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(rows[-1]["classification"], "renewal_prerequisites_missing")

    def test_other_business_error_never_renews(self):
        _, rows, calls, _ = self.execute([Response({}), Response({"errCode": -2041})])
        self.assertEqual(len(calls), 2)
        self.assertEqual(rows[-1]["renewal_attempts"], 0)

    def test_rejected_renewal_is_not_retried(self):
        result, rows, calls, _ = self.execute([
            Response({}), Response({"errCode": -2012}),
            Response({"errCode": -2012, "errMsg": MARKER}), Response({"errCode": -2012})])
        self.assertEqual(result, 2)
        self.assertEqual(rows[-1]["classification"], "shelf_not_recovered")
        self.assertEqual(len(calls), 4)

    def test_transport_exception_not_printed_or_retried(self):
        result, rows, calls, _ = self.execute([
            Response({}), Response({"errCode": -2012}), Response({"errCode": -2012})],
            post_error=True)
        self.assertEqual(result, 2)
        self.assertEqual(len(calls), 4)
        self.assertEqual(rows[-1]["renewal_attempts"], 1)

    def test_bootstrap_duplicate_cookie_reported_then_removed(self):
        fresh = requests.cookies.RequestsCookieJar()
        fresh.set("wr_skey", MARKER + "_NEW", domain="weread.qq.com", path="/")
        _, rows, _, _ = self.execute([
            Response({}, cookies=fresh), Response({"books": [], "bookProgress": [], "archive": []})])
        before = next(row for row in rows if row["probe"] == "after_bootstrap_cookies")
        after = next(row for row in rows if row["probe"] == "normalized_cookies")
        self.assertEqual(before["cookies"]["wr_skey"]["sent_count"], 2)
        self.assertEqual(after["cookies"]["wr_skey"]["sent_count"], 1)

    def test_response_body_and_noninteger_codes_never_leak(self):
        for payload in ({"errCode": MARKER}, {"succ": MARKER}, None, [MARKER], ...):
            with self.subTest(type=type(payload).__name__):
                self.assertNotIn(MARKER, json.dumps(probe.summarize(Response(payload), renewal=True)))


if __name__ == "__main__":
    unittest.main()
