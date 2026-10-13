"""Local browser-session helpers. This module never prints credential values."""

from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
import json
import math
from pathlib import Path


DEFAULT_STATE = Path("session.json")


def normalize_cookies(document) -> dict:
    """Accept storage-state JSON or a browser extension's cookie array."""
    items = document.get("cookies") if isinstance(document, dict) else document
    if not isinstance(items, list):
        raise ValueError("Ожидается JSON-массив cookies или объект с полем cookies")
    cookies = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Некорректная запись cookies")
        domain = item.get("domain", "")
        if not isinstance(domain, str):
            raise ValueError("Некорректный домен cookies")
        host = domain.lstrip(".").lower()
        if host != "yandex.ru" and not host.endswith(".yandex.ru"):
            continue
        if (not isinstance(item.get("name"), str) or not item["name"]
                or not isinstance(item.get("value"), str)):
            raise ValueError("Некорректное имя или значение cookies")
        expires = item.get("expirationDate", item.get("expires"))
        if item.get("session") is True or expires is None or expires == -1:
            expires = -1
        elif (isinstance(expires, bool) or not isinstance(expires, (int, float))
              or not math.isfinite(expires) or expires < 0):
            raise ValueError("Некорректный срок действия cookies")
        else:
            expires = int(expires)
        path = item.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("Некорректный путь cookies")
        cookies.append({
            "name": item["name"], "value": item["value"],
            "domain": domain.lower(), "path": path, "expires": expires,
            "secure": bool(item.get("secure", True)),
            "hostOnly": bool(item.get("hostOnly", not domain.startswith("."))),
            "httpOnly": bool(item.get("httpOnly", False)),
        })
    if not cookies:
        raise ValueError("В JSON нет cookies для yandex.ru")
    return {"cookies": cookies, "origins": []}


class ScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_script = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        self.in_script = tag == "script" and dict(attrs).get("id") == "preloaded-data"

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.parts.append(data)


def cookie_jar(state_path: Path) -> CookieJar:
    state = normalize_cookies(json.loads(state_path.read_text(encoding="utf-8-sig")))
    jar = CookieJar(policy=DefaultCookiePolicy(
        strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain,
    ))
    for item in state.get("cookies", []):
        domain = item["domain"]
        expires = item.get("expires", -1)
        jar.set_cookie(Cookie(
            version=0, name=item["name"], value=item["value"],
            port=None, port_specified=False, domain=domain,
            domain_specified=not item["hostOnly"], domain_initial_dot=domain.startswith("."),
            path=item.get("path", "/"), path_specified=True,
            secure=item.get("secure", True),
            expires=None if expires == -1 else int(expires),
            discard=expires == -1,
            comment=None, comment_url=None,
            rest={"HttpOnly": None} if item["httpOnly"] else {}, rfc2109=False,
        ))
    return jar


def find_config(value):
    if isinstance(value, dict):
        if isinstance(value.get("sk"), str) and value.get("idClient"):
            return value
        for nested in value.values():
            result = find_config(nested)
            if result:
                return result
    elif isinstance(value, list):
        for nested in value:
            result = find_config(nested)
            if result:
                return result
    return None
