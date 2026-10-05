import copy
import hashlib
import os
import re

import requests
from requests.utils import cookiejar_from_dict
from retrying import retry
from urllib.parse import quote, urlsplit

WEREAD_URL = "https://weread.qq.com/"
WEREAD_NOTEBOOKS_URL = "https://weread.qq.com/api/user/notebook"
WEREAD_BOOKMARKLIST_URL = "https://weread.qq.com/web/book/bookmarklist"
WEREAD_CHAPTER_INFO = "https://weread.qq.com/web/book/chapterInfos"
WEREAD_READ_INFO_URL = "https://weread.qq.com/web/book/readInfo"
WEREAD_REVIEW_LIST_URL = "https://weread.qq.com/web/review/list"
WEREAD_BOOK_INFO = "https://weread.qq.com/web/book/info"
WEREAD_SHELF_SYNC_URL = "https://weread.qq.com/web/shelf/sync"
WEREAD_RENEWAL_URL = "https://weread.qq.com/web/login/renewal"
WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Origin": "https://weread.qq.com",
    "Referer": WEREAD_URL,
    "Accept": "application/json, text/plain, */*",
}
WEREAD_READDATA_DETAIL = "https://i.weread.qq.com/readdata/detail"
WEREAD_HISTORY_URL = "https://i.weread.qq.com/readdata/summary?synckey=0"


class WeReadResponseError(RuntimeError):
    """A response failed validation; the message contains only safe metadata."""


class WeReadApi:
    def __init__(self):
        self.cookie = self.get_cookie()
        self.session = requests.Session()
        self.session.cookies = self.parse_cookie_string()
        self.session.headers.update(WEB_HEADERS)
        self._renewal_attempted = False
        self.normalize_cookies()

    def normalize_cookies(self, response=None):
        """Keep one host-scoped cookie per name, preferring freshly returned values."""
        selected = {}
        for cookie in self.session.cookies:
            if cookie.domain not in ("", "weread.qq.com", ".weread.qq.com"):
                continue
            old = selected.get(cookie.name)
            if old is None or cookie.domain or not old.domain:
                selected[cookie.name] = cookie
        if response is not None:
            for cookie in response.cookies:
                if cookie.domain in ("weread.qq.com", ".weread.qq.com"):
                    selected[cookie.name] = cookie
        jar = requests.cookies.RequestsCookieJar()
        for original in selected.values():
            cookie = copy.copy(original)
            cookie.domain = "weread.qq.com"
            cookie.domain_specified = True
            cookie.domain_initial_dot = False
            cookie.path = "/"
            cookie.path_specified = True
            cookie.secure = True
            if not cookie.is_expired():
                jar.set_cookie(cookie)
        self.session.cookies = jar

    def _send(self, method, url, endpoint, **kwargs):
        self.normalize_cookies()
        try:
            response = getattr(self.session, method)(
                url, timeout=15, allow_redirects=False, **kwargs
            )
        except requests.RequestException:
            # Transport exceptions can contain headers; never expose their message.
            raise WeReadResponseError(
                f"WeRead endpoint={endpoint}, reason=transport_error"
            ) from None
        self.normalize_cookies(response)
        return response

    def _request(self, method, url, endpoint, **kwargs):
        response = self._send(method, url, endpoint, **kwargs)
        try:
            data = response.json()
        except ValueError:
            return response
        if not isinstance(data, dict):
            return response
        codes = [data[key] for key in ("errCode", "errcode") if key in data]
        if (not codes or any(type(code) is not int for code in codes)
                or not any(code in (-2010, -2012) for code in codes)
                or any(code not in (0, -2010, -2012) for code in codes)):
            return response
        if getattr(self, "_renewal_attempted", False):
            return response
        if not all(self.session.cookies.get(name)
                   for name in ("wr_vid", "wr_skey", "wr_rt")):
            return response
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "weread.qq.com":
            return response
        # Set before sending: failures and outer retries cannot repeat renewal.
        self._renewal_attempted = True
        renewal = self._send(
            "post", WEREAD_RENEWAL_URL, "session_renewal",
            headers=WEB_HEADERS,
            json={"rq": quote(parts.path, safe=""),
                  "ql": self.session.cookies.get("wr_ql") == "1"},
        )
        renewed = self.validate_response(renewal, "session_renewal")
        if type(renewed.get("succ")) is not int or renewed["succ"] != 1:
            self.response_error(renewal, "session_renewal", "renewal_not_confirmed")
        print("WeRead session renewal=success; retrying original request once")
        return self._send(method, url, endpoint, **kwargs)

    def _bootstrap(self):
        self._send("get", WEREAD_URL, "homepage")

    def try_get_cloud_cookie(self, url, id, password):
        if url.endswith("/"):
            url = url[:-1]
        req_url = f"{url}/get/{id}"
        data = {"password": password}
        result = None
        response = requests.post(req_url, data=data)
        if response.status_code == 200:
            data = response.json()
            cookie_data = data.get("cookie_data")
            if cookie_data and "weread.qq.com" in cookie_data:
                cookies = cookie_data["weread.qq.com"]
                cookie_str = "; ".join(
                    [f"{cookie['name']}={cookie['value']}" for cookie in cookies]
                )
                result = cookie_str
        return result

    def get_cookie(self):
        url = os.getenv("CC_URL")
        if not url:
            url = "https://cookiecloud.malinkang.com/"
        id = os.getenv("CC_ID")
        password = os.getenv("CC_PASSWORD")
        cookie = os.getenv("WEREAD_COOKIE")
        if url and id and password:
            cookie = self.try_get_cloud_cookie(url, id, password)
        if not cookie or not cookie.strip():
            raise Exception("没有找到cookie，请按照文档填写cookie")
        return cookie

    def parse_cookie_string(self):
        cookies_dict = {}
        
        # 使用正则表达式解析 cookie 字符串
        pattern = re.compile(r'([^=]+)=([^;]+);?\s*')
        matches = pattern.findall(self.cookie)
        
        for key, value in matches:
            cookies_dict[key.strip()] = value.encode('unicode_escape').decode('ascii')
        # 直接使用 cookies_dict 创建 cookiejar
        cookiejar = cookiejar_from_dict(cookies_dict)
        
        return cookiejar

    def get_bookshelf(self):
        self._bootstrap()
        params = dict(synckey=0, teenmode=0, album=1, onlyBookid=0)
        r = self._request("get", WEREAD_SHELF_SYNC_URL, "bookshelf", params=params)
        data = self.validate_response(r, "bookshelf", ("books", "bookProgress", "archive"))
        # These records are consumed as mappings by book.main.
        for field in ("books", "bookProgress", "archive"):
            if not all(isinstance(item, dict) for item in data[field]):
                self.response_error(r, "bookshelf", "invalid_list_item", field)
        return data
        
    def handle_errcode(self,errcode):
        if( errcode== -2012 or errcode==-2010):
            print("::error::微信读书认证失败或会话失效，请重新验证登录状态。")

    def safe_error_context(self, response):
        """Return diagnostic metadata without logging response bodies or cookies."""
        errcode = None
        try:
            data = response.json()
            codes = ([data[key] for key in ("errCode", "errcode") if key in data]
                     if isinstance(data, dict) else [])
            # Prefer a nonzero code if both spellings are present. Never emit strings.
            numeric_codes = [code for code in codes if type(code) is int]
            if numeric_codes:
                errcode = next((code for code in numeric_codes if code != 0), numeric_codes[0])
            elif codes:
                errcode = "non_integer"
        except (ValueError, AttributeError):
            pass
        self.handle_errcode(errcode)
        return f"status={response.status_code}, errcode={errcode}"

    def response_error(self, response, endpoint, reason, field=None):
        # Labels are fixed at call sites; never include URLs, bodies or exception text.
        context = self.safe_error_context(response)
        suffix = f", field={field}" if field is not None else ""
        message = f"WeRead endpoint={endpoint}, {context}, reason={reason}{suffix}"
        print(f"::error::{message}")
        raise WeReadResponseError(message) from None

    def validate_response(self, response, endpoint, list_fields=()):
        try:
            data = response.json()
        except ValueError:
            self.response_error(response, endpoint, "invalid_json")
        if not isinstance(data, dict):
            self.response_error(response, endpoint, "invalid_json_object")
        codes = [data[key] for key in ("errCode", "errcode") if key in data]
        if any(type(code) is not int for code in codes):
            self.response_error(response, endpoint, "invalid_error_code_type")
        if any(code != 0 for code in codes):
            self.response_error(response, endpoint, "business_error")
        if not 200 <= response.status_code < 300:
            self.response_error(response, endpoint, "http_error")
        for field in list_fields:
            if field not in data:
                self.response_error(response, endpoint, "missing_field", field)
            if not isinstance(data[field], list):
                self.response_error(response, endpoint, "invalid_list_type", field)
        return data

    @retry(stop_max_attempt_number=3, wait_fixed=5000,
           retry_on_exception=lambda error: not isinstance(error, WeReadResponseError))
    def get_notebooklist(self):
        """获取笔记本列表"""
        self._bootstrap()
        r = self._request("get", WEREAD_NOTEBOOKS_URL, "notebooks")
        books = self.validate_response(r, "notebooks", ("books",))["books"]
        if not all(isinstance(book, dict) and type(book.get("sort")) in (int, float)
                   for book in books):
            self.response_error(r, "notebooks", "invalid_list_item", "books")
        return sorted(books, key=lambda book: book["sort"])

    @retry(stop_max_attempt_number=3, wait_fixed=5000,
           retry_on_exception=lambda error: not isinstance(error, WeReadResponseError))
    def get_bookinfo(self, bookId):
        """获取书的详情"""
        self._bootstrap()
        params = dict(bookId=bookId)
        r = self._request("get", WEREAD_BOOK_INFO, "book_info", params=params)
        return self.validate_response(r, "book_info")


    @retry(stop_max_attempt_number=3, wait_fixed=5000,
           retry_on_exception=lambda error: not isinstance(error, WeReadResponseError))
    def get_bookmark_list(self, bookId):
        self._bootstrap()
        params = dict(bookId=bookId)
        headers = {"Referer": self.get_url(bookId)}
        r = self._request("get", WEREAD_BOOKMARKLIST_URL, "bookmarks", params=params, headers=headers)
        return self.validate_response(r, "bookmarks", ("updated",))["updated"]

    @retry(stop_max_attempt_number=3, wait_fixed=5000,
           retry_on_exception=lambda error: not isinstance(error, WeReadResponseError))
    def get_read_info(self, bookId):
        self._bootstrap()
        params = dict(
            noteCount=1,
            readingDetail=1,
            finishedBookIndex=1,
            readingBookCount=1,
            readingBookIndex=1,
            finishedBookCount=1,
            bookId=bookId,
            finishedDate=1,
        )
        headers = {
            "baseapi":"32",
            "appver":"8.2.5.10163885",
            "basever":"8.2.5.10163885",
            "osver":"12",
            "User-Agent": "WeRead/8.2.5 WRBrand/xiaomi Dalvik/2.1.0 (Linux; U; Android 12; Redmi Note 7 Pro Build/SQ3A.220705.004)",
        }
        r = self._request("get", WEREAD_READ_INFO_URL, "read_info",headers=headers, params=params)
        return self.validate_response(r, "read_info")

    @retry(stop_max_attempt_number=3, wait_fixed=5000,
           retry_on_exception=lambda error: not isinstance(error, WeReadResponseError))
    def get_review_list(self, bookId):
        self._bootstrap()
        params = dict(bookId=bookId, listType=11, mine=1, syncKey=0)
        r = self._request("get", WEREAD_REVIEW_LIST_URL, "reviews", params=params)
        reviews = self.validate_response(r, "reviews", ("reviews",))["reviews"]
        if not all(isinstance(item, dict) and isinstance(item.get("review"), dict)
                   for item in reviews):
            self.response_error(r, "reviews", "invalid_list_item", "reviews")
        reviews = [item["review"] for item in reviews]
        return [{"chapterUid": 1000000, **item} if item.get("type") == 4 else item
                for item in reviews]



    
    def get_api_data(self):
        self._bootstrap()
        r = self._request("get", WEREAD_HISTORY_URL, "history")
        return self.validate_response(r, "history")

    

    @retry(stop_max_attempt_number=3, wait_fixed=5000,
           retry_on_exception=lambda error: not isinstance(error, WeReadResponseError))
    def get_chapter_info(self, bookId):
        self._bootstrap()
        body = {"bookIds": [bookId], "synckeys": [0], "teenmode": 0}
        r = self._request("post", WEREAD_CHAPTER_INFO, "chapters", json=body)
        data = self.validate_response(r, "chapters", ("data",))["data"]
        if (len(data) == 1 and isinstance(data[0], dict)
                and isinstance(data[0].get("updated"), list)
                and all(isinstance(item, dict) and "chapterUid" in item
                        for item in data[0]["updated"])):
            update = list(data[0]["updated"])
            update.append(
                {
                    "chapterUid": 1000000,
                    "chapterIdx": 1000000,
                    "updateTime": 1683825006,
                    "readAhead": 0,
                    "title": "点评",
                    "level": 1,
                }
            )
            return {item["chapterUid"]: item for item in update}
        else:
            self.response_error(r, "chapters", "invalid_chapter_data", "data")

    def transform_id(self, book_id):
        id_length = len(book_id)
        if re.match("^\\d*$", book_id):
            ary = []
            for i in range(0, id_length, 9):
                ary.append(format(int(book_id[i : min(i + 9, id_length)]), "x"))
            return "3", ary

        result = ""
        for i in range(id_length):
            result += format(ord(book_id[i]), "x")
        return "4", [result]

    def calculate_book_str_id(self, book_id):
        md5 = hashlib.md5()
        md5.update(book_id.encode("utf-8"))
        digest = md5.hexdigest()
        result = digest[0:3]
        code, transformed_ids = self.transform_id(book_id)
        result += code + "2" + digest[-2:]

        for i in range(len(transformed_ids)):
            hex_length_str = format(len(transformed_ids[i]), "x")
            if len(hex_length_str) == 1:
                hex_length_str = "0" + hex_length_str

            result += hex_length_str + transformed_ids[i]

            if i < len(transformed_ids) - 1:
                result += "g"

        if len(result) < 20:
            result += digest[0 : 20 - len(result)]

        md5 = hashlib.md5()
        md5.update(result.encode("utf-8"))
        result += md5.hexdigest()[0:3]
        return result

    def get_url(self, book_id):
        return f"https://weread.qq.com/web/reader/{self.calculate_book_str_id(book_id)}"
