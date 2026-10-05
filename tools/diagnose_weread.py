"""One bounded renewal experiment. Cookies remain in memory; no Notion imports."""
import copy
import json
import os
from pathlib import Path
import re
import sys

BASE = "https://weread.qq.com"
SHELF = BASE + "/web/shelf/sync"
RENEWAL = BASE + "/web/login/renewal"
PARAMS = {"synckey": 0, "teenmode": 0, "album": 1, "onlyBookid": 0}
NAMES = ("wr_vid", "wr_skey", "wr_rt", "wr_ql")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Origin": BASE,
    "Referer": BASE + "/",
    "Accept": "application/json, text/plain, */*",
}


def emit(probe, data):
    print(json.dumps(dict(data, probe=probe), ensure_ascii=True))


def summarize(response, fields=(), renewal=False):
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
    if renewal and "succ" in data:
        value = data["succ"]
        out["succ"] = value if type(value) in (int, bool) else "non_integer"
    if any(code in (-2010, -2012) for code in codes):
        label = "authentication_rejected"
    elif any(out.get(key) == "non_integer" for key in ("errCode", "errcode")):
        label = "unexpected_error_code_type"
    elif any(code != 0 for code in codes):
        label = "business_error"
    elif not 200 <= response.status_code < 300:
        label = "http_error"
    elif renewal:
        label = "renewal_success" if out.get("succ") in (1, True) else "renewal_not_confirmed"
    elif not all(meta["is_list"] for meta in out["fields"].values()):
        label = "response_shape_incomplete"
    else:
        label = "expected_shape"
    return dict(out, classification=label)


def metadata(session):
    import requests
    result = {}
    prepared = session.prepare_request(requests.Request("GET", SHELF, params=PARAMS))
    # Parse names in memory; never return/print the sensitive prepared header.
    sent_names = [part.split("=", 1)[0].strip()
                  for part in prepared.headers.get("Cookie", "").split(";")]
    for name in NAMES:
        cookies = [cookie for cookie in session.cookies if cookie.name == name]
        result[name] = {
            "present_nonempty": any(bool(cookie.value) for cookie in cookies),
            "jar_count": len(cookies),
            "sent_count": sent_names.count(name),
            "empty_domain_count": sum(cookie.domain == "" for cookie in cookies),
            "host_domain_count": sum(cookie.domain == "weread.qq.com" for cookie in cookies),
            "dot_domain_count": sum(cookie.domain == ".weread.qq.com" for cookie in cookies),
        }
    return result


def set_cookie_metadata(response):
    return {name: sum(cookie.name == name for cookie in response.cookies) for name in NAMES}


def normalize(session):
    import requests
    selected = {}
    for cookie in session.cookies:
        if cookie.domain not in ("", "weread.qq.com", ".weread.qq.com"):
            continue
        old = selected.get(cookie.name)
        # Domain cookies supplied by the server supersede imported domainless ones.
        if old is None or cookie.domain or not old.domain:
            selected[cookie.name] = cookie
    jar = requests.cookies.RequestsCookieJar()
    for cookie in selected.values():
        cookie = copy.copy(cookie)
        cookie.domain = "weread.qq.com"
        cookie.domain_specified = True
        cookie.domain_initial_dot = False
        cookie.path = "/"
        cookie.path_specified = True
        cookie.secure = True
        jar.set_cookie(cookie)
    session.cookies = jar


def shelf_probe(session):
    import requests
    try:
        response = session.get(SHELF, params=PARAMS, timeout=15, allow_redirects=False)
        result = summarize(response, ("books", "bookProgress", "archive"))
        result["set_cookie_counts"] = set_cookie_metadata(response)
        return result
    except requests.RequestException:
        return {"classification": "transport_error"}


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import requests
    from weread2notionpro.weread_api import WeReadApi
    cookie = os.getenv("WEREAD_COOKIE")
    if not cookie or not cookie.strip():
        emit("result", {"classification": "credential_not_configured", "renewal_attempts": 0})
        return 2
    api = WeReadApi.__new__(WeReadApi)
    api.cookie = cookie
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies = api.parse_cookie_string()
    raw_names = [key.strip() for key, _ in re.findall(r"([^=]+)=([^;]+);?\s*", cookie)]
    emit("initial_cookies", {"raw_name_counts": {name: raw_names.count(name) for name in NAMES},
                             "cookies": metadata(session)})
    try:
        response = session.get(BASE + "/", timeout=15, allow_redirects=False)
        emit("homepage", {"http_status": response.status_code,
                          "set_cookie_counts": set_cookie_metadata(response)})
    except requests.RequestException:
        emit("result", {"classification": "bootstrap_transport_error", "renewal_attempts": 0})
        return 2
    emit("after_bootstrap_cookies", {"cookies": metadata(session)})
    normalize(session)
    emit("normalized_cookies", {"cookies": metadata(session)})
    baseline = shelf_probe(session)
    emit("shelf_before_renewal", baseline)
    if baseline["classification"] == "expected_shape":
        emit("result", {"classification": "baseline_recovered_without_renewal", "renewal_attempts": 0})
        return 0
    if baseline["classification"] != "authentication_rejected":
        emit("result", {"classification": "baseline_not_authentication_error", "renewal_attempts": 0})
        return 2
    conditions = metadata(session)
    if not all(conditions[name]["present_nonempty"] for name in ("wr_vid", "wr_skey", "wr_rt")):
        emit("result", {"classification": "renewal_prerequisites_missing", "renewal_attempts": 0})
        return 2
    normalize(session)
    # Exactly one POST. No adapter retries, redirects, secret updates or persistence.
    try:
        response = session.post(RENEWAL,
                                json={"rq": "%2Fweb%2Fshelf%2Fsync", "ql": session.cookies.get("wr_ql") == "1"},
                                timeout=15, allow_redirects=False)
        renewal = summarize(response, renewal=True)
        renewal["set_cookie_counts"] = set_cookie_metadata(response)
        emit("renewal", renewal)
    except requests.RequestException:
        emit("renewal", {"classification": "transport_error"})
    emit("after_renewal_cookies", {"cookies": metadata(session)})
    normalize(session)
    emit("after_renewal_normalized_cookies", {"cookies": metadata(session)})
    after = shelf_probe(session)
    emit("shelf_after_renewal", after)
    recovered = after["classification"] == "expected_shape"
    emit("result", {"classification": "shelf_recovered_after_one_renewal" if recovered else "shelf_not_recovered",
                    "renewal_attempts": 1})
    return 0 if recovered else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Requests/JSON exceptions can carry sensitive content. Emit no exception value.
        emit("result", {"classification": "probe_internal_error"})
        sys.exit(2)
