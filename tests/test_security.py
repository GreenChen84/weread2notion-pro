import os
import unittest
from unittest.mock import MagicMock, patch

from weread2notionpro.notion_helper import (
    LEGACY_SECRET_PROPERTIES,
    NotionHelper,
)
from weread2notionpro.weread_api import (
    WEREAD_BOOKMARKLIST_URL,
    WEREAD_BOOK_INFO,
    WEREAD_CHAPTER_INFO,
    WEREAD_NOTEBOOKS_URL,
    WEREAD_READ_INFO_URL,
    WEREAD_REVIEW_LIST_URL,
    WEREAD_SHELF_SYNC_URL,
    WeReadApi,
)


class FakeResponse:
    status_code = 401

    def json(self):
        return {"errcode": -2010, "secret": "must-not-appear"}


class SecurityTests(unittest.TestCase):
    def test_setting_payload_never_contains_credentials(self):
        with patch.dict(
            os.environ,
            {
                "NOTION_TOKEN": "notion-secret",
                "NOTION_PAGE": "notion-page-secret",
                "WEREAD_COOKIE": "weread-secret",
            },
        ):
            payload = NotionHelper.build_setting_properties()

        serialized = repr(payload)
        self.assertNotIn("notion-secret", serialized)
        self.assertNotIn("notion-page-secret", serialized)
        self.assertNotIn("weread-secret", serialized)
        for name in LEGACY_SECRET_PROPERTIES:
            self.assertNotIn(name, payload)

    def test_existing_legacy_secret_fields_are_cleared(self):
        remote = {name: {"type": "rich_text"} for name in LEGACY_SECRET_PROPERTIES}
        self.assertEqual(
            NotionHelper.get_legacy_secret_clear_properties(remote),
            {name: {"rich_text": []} for name in LEGACY_SECRET_PROPERTIES},
        )

    def test_error_context_does_not_include_response_body(self):
        api = WeReadApi.__new__(WeReadApi)
        context = api.safe_error_context(FakeResponse())
        self.assertEqual(context, "status=401, errcode=-2010")
        self.assertNotIn("must-not-appear", context)

    def test_note_sync_uses_current_web_api_hosts(self):
        urls = (
            WEREAD_NOTEBOOKS_URL,
            WEREAD_BOOKMARKLIST_URL,
            WEREAD_CHAPTER_INFO,
            WEREAD_READ_INFO_URL,
            WEREAD_REVIEW_LIST_URL,
            WEREAD_BOOK_INFO,
            WEREAD_SHELF_SYNC_URL,
        )
        for url in urls:
            self.assertTrue(url.startswith("https://weread.qq.com/"), url)
            self.assertNotIn("https://i.weread.qq.com/", url)

    def test_bookmark_request_uses_reader_referer(self):
        api = WeReadApi.__new__(WeReadApi)
        api.session = MagicMock()
        response = MagicMock(ok=True)
        response.json.return_value = {"updated": []}
        api.session.get.return_value = response

        api.get_bookmark_list("123456")

        _, bookmark_call = api.session.get.call_args_list
        self.assertEqual(bookmark_call.args[0], WEREAD_BOOKMARKLIST_URL)
        self.assertEqual(
            bookmark_call.kwargs["headers"]["Referer"], api.get_url("123456")
        )


if __name__ == "__main__":
    unittest.main()
