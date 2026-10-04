import ast
import contextlib
import io
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from weread2notionpro.weread_api import WeReadApi, WeReadResponseError


class Response:
    def __init__(self, data, status=200):
        self.data = data
        self.status_code = status

    def json(self):
        if self.data is ...:
            raise ValueError("CREDENTIAL_SENTINEL")
        return self.data


class ResponseValidationTests(unittest.TestCase):
    def setUp(self):
        self.api = WeReadApi.__new__(WeReadApi)
        self.api.session = MagicMock()

    def assert_rejected(self, data, reason, status=200, method=None):
        response = Response(data, status)
        self.api.session.get.return_value = response
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(WeReadResponseError) as error:
            if method is None:
                self.api.validate_response(response, "bookshelf", ("books",))
            else:
                method()
        message = str(error.exception)
        self.assertIn(reason, message)
        self.assertIn("::error::", output.getvalue())
        self.assertNotIn("CREDENTIAL_SENTINEL", message + output.getvalue())
        return message, output.getvalue()

    def test_http_200_authentication_error_both_spellings(self):
        for key in ("errCode", "errcode"):
            for code in (-2010, -2012):
                with self.subTest(key=key, code=code):
                    message, output = self.assert_rejected(
                        {key: code, "errMsg": "CREDENTIAL_SENTINEL", "info": "CREDENTIAL_SENTINEL"},
                        "business_error", method=self.api.get_bookshelf)
                    self.assertIn(f"errcode={code}", message)
                    self.assertIn("认证失败或会话失效", output)

    def test_zero_code_does_not_mask_alternate_nonzero_code(self):
        message, _ = self.assert_rejected({"errCode": 0, "errcode": -2010}, "business_error")
        self.assertIn("errcode=-2010", message)

    def test_missing_progress_is_not_treated_as_empty(self):
        self.assert_rejected({"books": [], "archive": []}, "missing_field, field=bookProgress",
                             method=self.api.get_bookshelf)

    def test_null_or_wrong_progress_type_is_rejected(self):
        for value in (None, {}, "CREDENTIAL_SENTINEL"):
            with self.subTest(type=type(value).__name__):
                self.assert_rejected({"books": [], "archive": [], "bookProgress": value},
                                     "invalid_list_type", method=self.api.get_bookshelf)

    def test_explicit_empty_shelf_is_valid(self):
        data = {"books": [], "bookProgress": [], "archive": []}
        self.api.session.get.return_value = Response(data)
        self.assertIs(self.api.get_bookshelf(), data)

    def test_non_json_and_non_object_errors_remain_sanitized(self):
        self.assert_rejected(..., "invalid_json")
        for data in (None, [], ["errCode"], "CREDENTIAL_SENTINEL"):
            with self.subTest(type=type(data).__name__):
                self.assert_rejected(data, "invalid_json_object")

    def test_non_integer_error_codes_never_leak(self):
        for value in ("CREDENTIAL_SENTINEL", {"CREDENTIAL_SENTINEL": True}, False):
            with self.subTest(type=type(value).__name__):
                self.assert_rejected({"errCode": value}, "invalid_error_code_type")

    def test_http_error_with_valid_json(self):
        self.assert_rejected({"books": []}, "http_error", status=503)

    def test_notebooks_missing_books_rejected_and_valid_sort_preserved(self):
        # Retry delays are mocked; all requests are fake.
        with patch("retrying.time.sleep"):
            self.assert_rejected({}, "missing_field", method=self.api.get_notebooklist)
        self.api.session.get.return_value = Response({"books": [{"sort": 2}, {"sort": 1}]})
        self.assertEqual([book["sort"] for book in self.api.get_notebooklist()], [1, 2])

    def test_notebooks_bad_sort_is_rejected(self):
        with patch("retrying.time.sleep"):
            self.assert_rejected({"books": [{"sort": "CREDENTIAL_SENTINEL"}]}, "invalid_list_item",
                                 method=self.api.get_notebooklist)

    def test_reviews_and_chapters_reject_invalid_nested_records(self):
        with patch("retrying.time.sleep"):
            self.assert_rejected({"reviews": [{"review": None}]}, "invalid_list_item",
                                 method=lambda: self.api.get_review_list("test-book"))
            self.api.session.post.return_value = Response({"data": [{"updated": None}]})
            self.assert_rejected({}, "invalid_chapter_data",
                                 method=lambda: self.api.get_chapter_info("test-book"))

    def test_main_rejects_shelf_before_notion_initialization(self):
        self.assert_main_rejects_before_notion("get_bookshelf")

    def test_main_rejects_notebooks_before_notion_initialization(self):
        self.assert_main_rejects_before_notion("get_notebooklist")

    def assert_main_rejects_before_notion(self, failing_method):
        # Execute only the main function AST; importing book itself loads credentials.
        path = Path(__file__).resolve().parents[1] / "weread2notionpro/book.py"
        tree = ast.parse(path.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "main")
        api = MagicMock()
        getattr(api, failing_method).side_effect = WeReadResponseError("sanitized")
        constructor = MagicMock()
        namespace = {"weread_api": api, "NotionHelper": constructor}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        with self.assertRaises(WeReadResponseError):
            namespace["main"]()
        constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
