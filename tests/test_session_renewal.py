import contextlib
import io
import os
import unittest
from unittest.mock import patch

import requests

from weread2notionpro.weread_api import (
    WEREAD_CHAPTER_INFO, WEREAD_NOTEBOOKS_URL, WEREAD_RENEWAL_URL,
    WEREAD_SHELF_SYNC_URL, WeReadApi, WeReadResponseError,
)

SENTINEL = "CREDENTIAL_SENTINEL"
COOKIE = "wr_vid=oldvid; wr_skey=oldkey; wr_rt=oldrefresh; wr_ql=1"


def response(data, status=200, cookies=None):
    result = requests.Response()
    result.status_code = status
    result.cookies = requests.cookies.RequestsCookieJar()
    for name, value in (cookies or {}).items():
        result.cookies.set(name, value, domain=".weread.qq.com", path="/", secure=True)
    result.json = lambda: data
    return result


class SessionRenewalTests(unittest.TestCase):
    def setUp(self):
        with patch.object(WeReadApi, "get_cookie", return_value=COOKIE):
            self.api = WeReadApi()
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def test_authentication_spellings_and_codes_renew_once_and_repeat_exact_get(self):
        for key in ("errCode", "errcode"):
            for code in (-2010, -2012):
                with self.subTest(key=key, code=code):
                    self.api._renewal_attempted = False
                    valid = response({"books": [], "bookProgress": [], "archive": []})
                    with patch.object(self.api.session, "get", side_effect=[response({key: code}), valid]) as get, \
                         patch.object(self.api.session, "post", return_value=response({"succ": 1})) as post:
                        result = self.api._request("get", WEREAD_SHELF_SYNC_URL, "bookshelf", params={"synckey": 0})
                    self.assertIs(result, valid)
                    self.assertEqual(get.call_count, 2)
                    self.assertEqual(get.call_args_list[0], get.call_args_list[1])
                    self.assertEqual(post.call_count, 1)
                    self.assertEqual(post.call_args.args, (WEREAD_RENEWAL_URL,))
                    self.assertEqual(post.call_args.kwargs["json"], {"rq": "%2Fweb%2Fshelf%2Fsync", "ql": True})
                    self.assertEqual(post.call_args.kwargs["timeout"], 15)
                    self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_post_request_body_is_repeated_once_after_renewal(self):
        good = response({"data": []})
        with patch.object(self.api.session, "post", side_effect=[response({"errCode": -2010}), response({"succ": 1}), good]) as post:
            result = self.api._request("post", WEREAD_CHAPTER_INFO, "chapters", json={"bookIds": ["test"]})
        self.assertIs(result, good)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(post.call_args_list[0], post.call_args_list[2])

    def test_second_rejection_and_later_requests_do_not_renew_again(self):
        denied = response({"errCode": -2012})
        with patch.object(self.api.session, "get", return_value=denied) as get, \
             patch.object(self.api.session, "post", return_value=response({"succ": 1})) as post:
            for _ in range(2):
                result = self.api._request("get", WEREAD_SHELF_SYNC_URL, "bookshelf")
                with self.assertRaises(WeReadResponseError):
                    self.api.validate_response(result, "bookshelf")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 3)

    def test_decorated_notebook_method_cannot_repeat_authentication_attempt(self):
        with patch.object(self.api, "_bootstrap"), \
             patch.object(self.api.session, "get", return_value=response({"errCode": -2012})) as get, \
             patch.object(self.api.session, "post", return_value=response({"succ": 1})) as post, \
             patch("retrying.time.sleep") as sleep:
            with self.assertRaises(WeReadResponseError):
                self.api.get_notebooklist()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 2)
        sleep.assert_not_called()

    def test_non_authentication_or_ambiguous_errors_never_renew(self):
        for data in ({"books": []}, {"errCode": 0}, {"errCode": -500},
                     {"errCode": SENTINEL}, {"errCode": False},
                     {"errCode": -2012, "errcode": -500}, []):
            with self.subTest(data=data), \
                 patch.object(self.api.session, "get", return_value=response(data)), \
                 patch.object(self.api.session, "post") as post:
                self.api._request("get", WEREAD_SHELF_SYNC_URL, "bookshelf")
                post.assert_not_called()

    def test_missing_refresh_cookie_never_renews(self):
        self.api.session.cookies.clear(domain="weread.qq.com", path="/", name="wr_rt")
        with patch.object(self.api.session, "get", return_value=response({"errCode": -2012})), \
             patch.object(self.api.session, "post") as post:
            self.api._request("get", WEREAD_SHELF_SYNC_URL, "bookshelf")
        post.assert_not_called()

    def test_failed_renewal_is_sanitized_and_never_retried(self):
        for data in ({"succ": 0, "errMsg": SENTINEL}, {"succ": SENTINEL},
                     {"succ": True}, {"succ": 1, "errCode": -2010}):
            with self.subTest(data=data):
                self.api._renewal_attempted = False
                with patch.object(self.api.session, "get", return_value=response({"errCode": -2012})) as get, \
                     patch.object(self.api.session, "post", return_value=response(data)) as post:
                    with self.assertRaises(WeReadResponseError) as error:
                        self.api._request("get", WEREAD_SHELF_SYNC_URL, "bookshelf")
                    self.api._request("get", WEREAD_SHELF_SYNC_URL, "bookshelf")
                self.assertEqual(post.call_count, 1)
                self.assertEqual(get.call_count, 2)
                self.assertNotIn(SENTINEL, str(error.exception) + self.output.getvalue())

    def test_transport_failure_does_not_leak_or_retry(self):
        with patch.object(self.api.session, "get", return_value=response({"errCode": -2012})), \
             patch.object(self.api.session, "post", side_effect=requests.ConnectionError(SENTINEL)) as post:
            with self.assertRaises(WeReadResponseError) as error:
                self.api._request("get", WEREAD_SHELF_SYNC_URL, "bookshelf")
            self.api._request("get", WEREAD_SHELF_SYNC_URL, "bookshelf")
        self.assertEqual(post.call_count, 1)
        self.assertNotIn(SENTINEL, str(error.exception) + self.output.getvalue())
        self.assertTrue(error.exception.__suppress_context__)

    def test_fresh_cookie_overrides_duplicates_and_sent_values_are_unique(self):
        jar = self.api.session.cookies
        jar.set("wr_skey", "old-domainless", domain="", path="/")
        jar.set("wr_skey", "old-dot", domain=".weread.qq.com", path="/")
        with patch.object(self.api.session, "get", return_value=response({}, cookies={"wr_skey": SENTINEL})):
            self.api._send("get", WEREAD_SHELF_SYNC_URL, "bookshelf")
        prepared = self.api.session.prepare_request(requests.Request("GET", WEREAD_SHELF_SYNC_URL))
        names = [part.split("=", 1)[0].strip() for part in prepared.headers["Cookie"].split(";")]
        self.assertEqual(names.count("wr_skey"), 1)
        self.assertEqual(self.api.session.cookies.get("wr_skey"), SENTINEL)
        self.assertTrue(all(cookie.domain == "weread.qq.com" and cookie.secure
                            for cookie in self.api.session.cookies))
        foreign = self.api.session.prepare_request(requests.Request("GET", "https://example.com/"))
        self.assertNotIn("Cookie", foreign.headers)
        self.assertNotIn(SENTINEL, self.output.getvalue())

    def test_renewed_cookies_used_for_retry_and_only_remain_in_memory(self):
        saved_environment = dict(os.environ)
        def get(*args, **kwargs):
            if self.api.session.cookies.get("wr_skey") == SENTINEL:
                return response({"books": []})
            return response({"errCode": -2012})
        with patch.object(self.api.session, "get", side_effect=get), \
             patch.object(self.api.session, "post", return_value=response({"succ": 1}, cookies={"wr_skey": SENTINEL, "wr_rt": "newrefresh"})):
            result = self.api._request("get", WEREAD_NOTEBOOKS_URL, "notebooks")
        self.assertEqual(result.json(), {"books": []})
        self.assertEqual(self.api.session.cookies.get("wr_rt"), "newrefresh")
        self.assertEqual(self.api.cookie, COOKIE)
        self.assertEqual(dict(os.environ), saved_environment)
        self.assertNotIn(SENTINEL, self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
