import os
import unittest
from unittest.mock import patch

from weread2notionpro.notion_helper import (
    LEGACY_SECRET_PROPERTIES,
    NotionHelper,
)
from weread2notionpro.weread_api import WeReadApi


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


if __name__ == "__main__":
    unittest.main()
