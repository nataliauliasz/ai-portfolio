from __future__ import annotations

import io
import ssl
import xml.etree.ElementTree as ET
from base64 import b64encode
from typing import Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

try:
    import requests
    from requests import exceptions as requests_exceptions
except ImportError:  # pragma: no cover - optional dependency
    requests = None
    requests_exceptions = None

PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    "<propfind xmlns='DAV:'>"
    "<prop><displayname/><getcontentlength/><resourcetype/></prop>"
    "</propfind>"
)


def ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def normalized_path(path: str) -> str:
    if not path:
        path = "/"
    return path if path.endswith("/") else path + "/"


def build_auth_header(username: Optional[str], password: Optional[str]) -> Optional[str]:
    if not username:
        return None
    token = f"{username}:{password or ''}".encode("utf-8")
    return "Basic " + b64encode(token).decode("ascii")


def fetch_directory_xml_basic(
    url: str,
    timeout: float,
    context: ssl.SSLContext,
    auth_header: Optional[str],
) -> bytes:
    request = Request(
        url,
        data=PROPFIND_BODY.encode("utf-8"),
        method="PROPFIND",
    )
    request.add_header("Depth", "1")
    request.add_header("Content-Type", "text/xml; charset=utf-8")
    if auth_header:
        request.add_header("Authorization", auth_header)
    with urlopen(request, timeout=timeout, context=context) as response:
        return response.read()


def fetch_directory_xml_negotiate(
    url: str,
    timeout: float,
    verify_ssl: bool,
    username: Optional[str],
    password: Optional[str],
) -> bytes:
    if requests is None:
        raise RuntimeError("Pakiet 'requests' jest wymagany dla uwierzytelniania Negotiate.")
    try:
        from requests_negotiate_sspi import HttpNegotiateAuth
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError(
            "Zainstaluj 'requests-negotiate-sspi', aby korzystac z uwierzytelniania Negotiate."
        ) from exc

    headers = {
        "Depth": "1",
        "Content-Type": "text/xml; charset=utf-8",
    }
    auth = (
        HttpNegotiateAuth(username=username, password=password)
        if username
        else HttpNegotiateAuth()
    )

    if not verify_ssl:
        try:
            from urllib3.exceptions import InsecureRequestWarning

            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        except Exception:  # pragma: no cover - defensive
            pass

    try:
        response = requests.request(
            "PROPFIND",
            url,
            data=PROPFIND_BODY.encode("utf-8"),
            headers=headers,
            timeout=timeout,
            verify=verify_ssl,
            auth=auth,
        )
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        resp = exc.response
        body = resp.content if resp is not None else b""
        hdrs = resp.headers if resp is not None else {}
        code = resp.status_code if resp is not None else 0
        reason = resp.reason if resp is not None else str(exc)
        url_used = resp.url if resp is not None else url
        raise HTTPError(
            url_used,
            code,
            reason,
            hdrs,
            io.BytesIO(body),
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise URLError(str(exc)) from exc

    return response.content


def make_fetcher(
    auth_method: str,
    timeout: float,
    context: ssl.SSLContext,
    auth_header: Optional[str],
    verify_ssl: bool,
    username: Optional[str],
    password: Optional[str],
) -> Callable[[str], bytes]:
    if auth_method == "basic":
        return lambda current_url: fetch_directory_xml_basic(
            current_url, timeout, context, auth_header
        )
    if auth_method == "negotiate":
        return lambda current_url: fetch_directory_xml_negotiate(
            current_url, timeout, verify_ssl, username, password
        )
    raise ValueError(f"Nieznana metoda uwierzytelnienia: {auth_method}")


def _successful_prop(response_node):
    ns = "{DAV:}"
    for propstat in response_node.findall(f"{ns}propstat"):
        status = propstat.find(f"{ns}status")
        if status is None or "200" in status.text:
            return propstat.find(f"{ns}prop")
    return None


def parse_propfind_response(xml_data: bytes, base_path: str):
    ns = "{DAV:}"
    base_path = normalized_path(base_path)
    items = []

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        raise ValueError(f"Nie udalo sie sparsowac odpowiedzi PROPFIND: {exc}") from exc

    for response in root.findall(f"{ns}response"):
        href_el = response.find(f"{ns}href")
        if href_el is None or not href_el.text:
            continue

        parsed_href = urlparse(unquote(href_el.text))
        path = parsed_href.path or "/"

        if path.rstrip("/") == base_path.rstrip("/"):
            continue
        if not path.startswith(base_path):
            continue

        rel_path = path[len(base_path) :]
        rel_path = rel_path.lstrip("/")
        if not rel_path:
            continue

        prop_node = _successful_prop(response)
        if prop_node is None:
            continue

        res_type = prop_node.find(f"{ns}resourcetype")
        is_dir = res_type is not None and res_type.find(f"{ns}collection") is not None

        name = rel_path.rstrip("/") if is_dir else rel_path
        items.append({"name": name, "is_dir": is_dir})

    return items


def list_remote_svn(
    url: str,
    recursive: bool = True,
    timeout: float = 15.0,
    verify_ssl: bool = True,
    username: Optional[str] = None,
    password: Optional[str] = None,
    auth_method: str = "basic",
) -> List[str]:
    """
    Zwraca liste plikow (sciezki wzgledem URL) z repozytorium SVN korzystajac z PROPFIND.
    """
    context = ssl._create_unverified_context() if not verify_ssl else ssl.create_default_context()
    auth_header = build_auth_header(username, password) if auth_method == "basic" else None
    fetch_xml = make_fetcher(
        auth_method=auth_method,
        timeout=timeout,
        context=context,
        auth_header=auth_header,
        verify_ssl=verify_ssl,
        username=username,
        password=password,
    )

    collected: List[str] = []
    base_url = ensure_trailing_slash(url)

    def walk(current_url: str, prefix: str) -> None:
        xml_data = fetch_xml(current_url)
        base_path = urlparse(current_url).path
        entries = parse_propfind_response(xml_data, base_path)
        for entry in entries:
            display_name = prefix + entry["name"]
            if entry["is_dir"]:
                if recursive:
                    encoded = quote(entry["name"])
                    next_url = urljoin(current_url, ensure_trailing_slash(encoded))
                    walk(next_url, display_name + "/")
            else:
                collected.append(display_name)

    walk(base_url, "")
    return collected
