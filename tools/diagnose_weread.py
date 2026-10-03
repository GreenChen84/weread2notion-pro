"""GET-only probes; never import book/weread or connect to Notion."""
import json
import os
from pathlib import Path
import sys


def summarize(response, fields):
    out = {"http_status": response.status_code}
    try:
        data = response.json()
    except ValueError:
        return dict(out, classification="non_json")
    if not isinstance(data, dict):
        return dict(out, classification="unexpected_json_type")
    codes = []
    for key in ("errCode", "errcode"):
        if key in data:
            value = data[key]
            out[key] = value if type(value) is int else "non_integer"
            if type(value) is int:
                codes.append(value)
    out["fields"] = {}
    for field in fields:
        value = data.get(field)
        meta = {"present": field in data, "is_list": isinstance(value, list)}
        if isinstance(value, list):
            meta["count"] = len(value)
        out["fields"][field] = meta
    if any(code in (-2010, -2012) for code in codes):
        label = "authentication_rejected"
    elif any(out.get(key) == "non_integer" for key in ("errCode", "errcode")):
        label = "unexpected_error_code_type"
    elif any(code != 0 for code in codes):
        label = "business_error"
    elif not 200 <= response.status_code < 300:
        label = "http_error"
    elif not all(meta["is_list"] for meta in out["fields"].values()):
        label = "response_shape_incomplete"
    else:
        label = "expected_shape"
    return dict(out, classification=label)


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import requests
    from weread2notionpro.weread_api import WeReadApi
    cookie = os.getenv("WEREAD_COOKIE")
    if not cookie or not cookie.strip():
        print(json.dumps({"classification": "credential_not_configured"}))
        return 2
    # Bypass get_cookie/CookieCloud; reuse only the existing parser.
    api = WeReadApi.__new__(WeReadApi)
    api.cookie = cookie
    session = requests.Session()
    session.cookies = api.parse_cookie_string()
    print(json.dumps({"probe": "credential_shape", "wr_vid_present": "wr_vid" in session.cookies,
                      "wr_skey_present": "wr_skey" in session.cookies}))
    response = session.get("https://weread.qq.com/", timeout=15, allow_redirects=False)
    print(json.dumps({"probe": "homepage", "http_status": response.status_code}))
    probes = (
        ("shelf", "https://weread.qq.com/web/shelf/sync",
         {"synckey": 0, "teenmode": 0, "album": 1, "onlyBookid": 0},
         ("books", "bookProgress", "archive")),
        ("notebooks", "https://weread.qq.com/api/user/notebook", None, ("books",)),
    )
    failed = False
    for name, url, params, fields in probes:
        try:
            response = session.get(url, params=params, timeout=15, allow_redirects=False)
            result = summarize(response, fields)
        except requests.RequestException:
            result = {"classification": "transport_error"}
        result["probe"] = name
        print(json.dumps(result))
        failed |= result["classification"] != "expected_shape"
    return 2 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Exception values may contain request details: never emit them.
        print(json.dumps({"classification": "probe_internal_error"}))
        sys.exit(2)
