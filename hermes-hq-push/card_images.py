"""Card photo checks and discovery (0.9.93): keeps product and hotel cards from showing blank placeholders.

Product cards (`show_products`, shopping.py) and hotel cards (`show_hotels`, travel.py) are drawn from the tool call's
own arguments in the Hermes HQ app, so the photo URL must be in the call itself. This module does three small jobs:

1. Static checks (`bad_image_reason`): catches the mistakes that always turn into a blank placeholder — passing the
   product/hotel page URL itself, an SVG (the app's RemoteImageFetcher refuses SVGs), a favicon, or a generic
   placeholder/share banner.
2. Safe live probe (`probe_image`): on live tool calls, verifies a supplied photo URL does not definitively return
   404/410 or HTML/SVG markup. Bot-protection responses (403/429) and transient timeouts fail open so valid CDN photos
   that iOS/Mac URLSession loads cleanly are never falsely blocked.
3. Safe photo discovery (`discover_photo`): when a call is refused for a missing or broken photo, reads up to 256 KiB
   of the store/hotel page (and product offers) with public-IP SSRF checks and no cookies, extracts JSON-LD `image` and
   Open Graph / Twitter Card image URLs, verifies one returns a real raster image, and hands that URL back to the bot
   in the refusal message so it can retry cleanly.
"""
from __future__ import annotations

import concurrent.futures
import html as html_lib
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

MAX_HTML_BYTES = 256 * 1024
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = 3.5
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
IMAGE_ACCEPT = "image/webp,image/png,image/jpeg,image/*;q=0.8"
HTML_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_PRIVATE_SUFFIXES = (".localhost", ".local", ".ts.net", ".internal", ".lan", ".home.arpa")
_SVG_QUERY = re.compile(r"(?:^|[?&])(?:format|fm|f|ext|type)=svgz?(?:&|$)", re.IGNORECASE)
_ICON_PATH = re.compile(r"(?:^|[/_.-])(?:favicon|apple-touch-icon|mask-icon|mstile)(?:[._/-]|$)", re.IGNORECASE)
_BANNER_PATH = re.compile(
    r"(?:instacart[_-]?share[_-]?banner|share[_-]?banner|og[_-]?default|default[_-]?og|"
    r"no[_-]?image|noimage|image[_-]?not[_-]?found|placeholder|spacer\.gif|blank\.gif|1x1\.)",
    re.IGNORECASE,
)
_LOGO_BASE = re.compile(
    r"^(?:(?:site|brand|store|company|header|footer|nav)[_-])?logo"
    r"(?:[_-]?(?:dark|light|white|black|color|colour|header|footer|nav|main|default|primary|"
    r"small|large|sm|md|lg|icon|mark|wordmark|\d+x\d+|\d+))*(?:\.[a-z0-9]+)?$",
    re.IGNORECASE,
)

_SCRIPT_LD_RE = re.compile(
    r"<script\b[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_BASE_TAG_RE = re.compile(r"<base\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""\b([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""")

_LD_TYPES = frozenset({
    "product", "individualproduct", "productgroup", "productmodel", "offer",
    "hotel", "lodgingbusiness", "resort", "motel", "inn", "bedandbreakfast", "hostel",
    "vacationrental", "accommodation",
})
_META_IMAGE_KEYS = ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src")


def _norm_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def bad_image_reason(image_url: str, page_url: str = "") -> str | None:
    """A plain reason when `image_url` is obviously not a displayable product/hotel photo, else None."""
    if not isinstance(image_url, str) or not image_url.strip():
        return None
    raw = image_url.strip()
    if page_url and _norm_url(raw) == _norm_url(page_url):
        return "is the page link itself, not a photo — pass a direct image URL"
    parts = urllib.parse.urlsplit(raw)
    path = parts.path.lower()
    if path.endswith(".svg") or path.endswith(".svgz") or _SVG_QUERY.search(parts.query):
        return "is an SVG, which cards cannot display — pass a JPEG, PNG or WebP photo URL"
    if _ICON_PATH.search(path):
        return "is a site icon, not a real photo"
    if _BANNER_PATH.search(path):
        return "looks like a generic placeholder or site share banner, not the item's own photo"
    base = path.rsplit("/", 1)[-1]
    if base and _LOGO_BASE.match(base):
        return "looks like a site logo, not the item's own photo"
    return None


def is_public_ip(address: str) -> bool:
    """True only for public unicast IPv4/IPv6 addresses (no loopback, RFC 1918, CGNAT/Tailscale, ULA, link-local)."""
    bare = str(address or "").split("%", 1)[0].strip()
    if not bare:
        return False
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return False
    return bool(ip.is_global and not ip.is_private and not ip.is_loopback and not ip.is_link_local
                and not ip.is_multicast and not ip.is_reserved and not ip.is_unspecified)


def _default_resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    out: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr and addr not in out:
            out.append(addr)
    return out


def is_public_https(url: str, resolver: Callable[[str], list[str]] | None = None) -> bool:
    """True when `url` is https on a host that resolves exclusively to public internet addresses."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() != "https" or not parts.hostname:
        return False
    host = parts.hostname.lower().strip("[]")
    if not host or host == "localhost" or any(host.endswith(s) for s in _PRIVATE_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host)
        return is_public_ip(host)
    except ValueError:
        if "." not in host:
            return False
    addrs = (resolver or _default_resolve)(host)
    return bool(addrs) and all(is_public_ip(a) for a in addrs)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, resolver: Callable[[str], list[str]] | None = None):
        super().__init__()
        self._resolver = resolver
        self._hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._hops += 1
        if self._hops > MAX_REDIRECTS:
            raise urllib.error.URLError("too many redirects")
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        if not is_public_https(resolved, resolver=self._resolver):
            raise urllib.error.URLError("redirect refused")
        nxt = super().redirect_request(req, fp, code, msg, headers, resolved)
        if nxt is not None:
            for h in ("Cookie", "Authorization"):
                if h in nxt.headers:
                    del nxt.headers[h]
        return nxt


def _open(url: str, accept: str, timeout: float, resolver: Callable[[str], list[str]] | None = None):
    if not is_public_https(url, resolver=resolver):
        raise urllib.error.URLError("not a public https host")
    opener = urllib.request.build_opener(_SafeRedirect(resolver=resolver))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    return opener.open(req, timeout=timeout)


def is_raster_magic(head: bytes) -> bool:
    """True when `head` begins with JPEG, PNG, GIF, WebP or ISO-BMFF (AVIF/HEIC) image magic bytes."""
    if len(head) < 12:
        return False
    if head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff"):
        return True
    if head.startswith((b"GIF87a", b"GIF89a")):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis", b"heic", b"heix", b"mif1", b"msf1"):
        return True
    return False


def _looks_like_markup(head: bytes) -> bool:
    s = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return s.startswith((b"<!doctype", b"<html", b"<head", b"<body", b"<svg", b"<?xml"))


def probe_image(url: str, *, timeout: float = DEFAULT_TIMEOUT, require_ok: bool = False,
                opener: Callable[..., object] | None = None,
                resolver: Callable[[str], list[str]] | None = None) -> str | None:
    """Verify an image URL. Returns a refusal reason only when definitively broken/non-image (or unverified if
    `require_ok` is True), else None."""
    try:
        resp = opener(url, IMAGE_ACCEPT, timeout) if opener else _open(url, IMAGE_ACCEPT, timeout, resolver=resolver)
        with resp:
            status = getattr(resp, "status", None) or getattr(resp, "code", 200)
            headers = getattr(resp, "headers", None) or {}
            ctype = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            head = resp.read(512)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return f"returned HTTP {exc.code} (not found)"
        return f"returned HTTP {exc.code}" if require_ok else None
    except Exception:
        return "could not be reached" if require_ok else None

    if status in (404, 410):
        return f"returned HTTP {status} (not found)"
    if not (200 <= status < 300):
        return f"returned HTTP {status}" if require_ok else None
    if "svg" in ctype:
        return "returned an SVG instead of a raster photo"
    if ctype.startswith(("text/", "application/json", "application/xml", "application/xhtml")):
        return f"returned {ctype} instead of a photo"
    if _looks_like_markup(head):
        return "returned markup instead of a photo"
    if require_ok and not (ctype.startswith("image/") or is_raster_magic(head)):
        return f"returned {ctype or 'non-image content'} instead of a photo"
    return None


def _tag_attrs(tag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _ATTR_RE.finditer(tag):
        key = match.group(1).lower()
        val = match.group(2) if match.group(2) is not None else (
            match.group(3) if match.group(3) is not None else (match.group(4) or ""))
        out[key] = html_lib.unescape(val.strip())
    return out


def _abs_https(raw: str, base: str) -> str:
    clean = html_lib.unescape(str(raw or "").strip())
    if not clean:
        return ""
    joined = urllib.parse.urljoin(base, clean)
    parts = urllib.parse.urlsplit(joined)
    if parts.scheme.lower() != "https" or not parts.netloc or "." not in (parts.hostname or ""):
        return ""
    if any(ch.isspace() for ch in joined):
        return ""
    return joined


def _collect_ld_urls(value, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, list):
        for item in value:
            _collect_ld_urls(item, out)
    elif isinstance(value, dict):
        for k in ("url", "contentUrl", "@id"):
            if isinstance(value.get(k), str):
                out.append(value[k])
                break


def _walk_ld(node, primary: list[str], secondary: list[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_ld(item, primary, secondary)
        return
    if not isinstance(node, dict):
        return
    raw_type = node.get("@type")
    types = raw_type if isinstance(raw_type, list) else ([raw_type] if isinstance(raw_type, str) else [])
    matched = any(str(t).rsplit("/", 1)[-1].lower() in _LD_TYPES for t in types)
    bucket = primary if matched else secondary
    for key in ("image", "photo", "photos"):
        if key in node:
            _collect_ld_urls(node[key], bucket)
    for child in node.values():
        if isinstance(child, (dict, list)):
            _walk_ld(child, primary, secondary)


def extract_page_images(html_text: str, page_url: str) -> list[str]:
    """Candidate https photo URLs from a product/hotel page (JSON-LD first, then Open Graph / Twitter Card)."""
    if not html_text:
        return []
    base = page_url
    base_match = _BASE_TAG_RE.search(html_text)
    if base_match:
        href = _tag_attrs(base_match.group(0)).get("href", "")
        resolved_base = _abs_https(href, page_url)
        if resolved_base:
            base = resolved_base

    ld_primary: list[str] = []
    ld_secondary: list[str] = []
    for match in _SCRIPT_LD_RE.finditer(html_text):
        body = match.group(1).strip()
        if not body:
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        _walk_ld(data, ld_primary, ld_secondary)

    meta_by_key: dict[str, list[str]] = {k: [] for k in _META_IMAGE_KEYS}
    for match in _META_TAG_RE.finditer(html_text):
        attrs = _tag_attrs(match.group(0))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in meta_by_key and attrs.get("content"):
            meta_by_key[key].append(attrs["content"])

    link_images: list[str] = []
    for match in _LINK_TAG_RE.finditer(html_text):
        attrs = _tag_attrs(match.group(0))
        if attrs.get("rel", "").lower() == "image_src" and attrs.get("href"):
            link_images.append(attrs["href"])

    ordered = (ld_primary
               + [u for k in _META_IMAGE_KEYS for u in meta_by_key[k]]
               + link_images
               + ld_secondary)
    seen: set[str] = set()
    out: list[str] = []
    for raw in ordered:
        url = _abs_https(raw, base)
        if not url or url in seen or bad_image_reason(url, page_url):
            continue
        seen.add(url)
        out.append(url)
    return out


def _fetch_html(url: str, timeout: float, opener=None, resolver=None) -> tuple[str, str]:
    resp = opener(url, HTML_ACCEPT, timeout) if opener else _open(url, HTML_ACCEPT, timeout, resolver=resolver)
    with resp:
        status = getattr(resp, "status", None) or getattr(resp, "code", 200)
        if not (200 <= status < 300):
            return "", url
        headers = getattr(resp, "headers", None) or {}
        ctype = str(headers.get("Content-Type") or "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype:
            return "", url
        final_url = getattr(resp, "url", None) or (resp.geturl() if hasattr(resp, "geturl") else url) or url
        data = resp.read(MAX_HTML_BYTES)
    return data.decode("utf-8", errors="replace"), str(final_url)


def discover_photo(page_urls: list[str], *, timeout: float = DEFAULT_TIMEOUT,
                   opener: Callable[..., object] | None = None,
                   resolver: Callable[[str], list[str]] | None = None) -> str | None:
    """Try `page_urls` in order and return the first verified raster photo URL found in their metadata."""
    seen: set[str] = set()
    for page_url in page_urls:
        if not isinstance(page_url, str) or not page_url.strip() or page_url in seen:
            continue
        seen.add(page_url)
        if len(seen) > 4:
            break
        try:
            html_text, final_url = _fetch_html(page_url, timeout, opener=opener, resolver=resolver)
        except Exception:
            continue
        if not html_text:
            continue
        for candidate in extract_page_images(html_text, final_url)[:3]:
            if probe_image(candidate, timeout=timeout, require_ok=True, opener=opener, resolver=resolver) is None:
                return candidate
    return None


def missing_photo_message(where: str, kind: str, hint: str | None = None) -> str:
    """Refusal text when an item has no photo, optionally carrying a verified candidate found on its page."""
    if hint:
        return (f"{where}`image` is missing — every {kind} card needs a real photo. Found this image on its page: "
                f"{hint} — verify it shows this {kind} and pass it as `image`, or get the {kind}'s own photo URL "
                "from the page.")
    return (f"{where}`image` is missing — every {kind} card must include a real photo URL (`https://...`). "
            f"Check the markdown image links from `web_extract`, open the {kind}'s page in the browser "
            "(`browser_exec` / `browser_get_images`), or look up the exact photo URL on the site, then call again.")


def verify_and_hint(items: list[dict], *, kind: str = "product", timeout: float = DEFAULT_TIMEOUT,
                    opener: Callable[..., object] | None = None,
                    resolver: Callable[[str], list[str]] | None = None) -> list[str]:
    """Live check across items: hints missing photos and refuses definitively broken/non-image supplied photos."""
    if not items:
        return []

    def check_one(item: dict) -> str | None:
        where = item["where"]
        images: list[str] = item.get("images") or []
        pages: list[str] = item.get("pages") or []
        if not images:
            hint = discover_photo(pages, timeout=timeout, opener=opener, resolver=resolver)
            return missing_photo_message(where, kind, hint=hint)
        reason = probe_image(images[0], timeout=timeout, require_ok=False, opener=opener, resolver=resolver)
        if reason is None:
            return None
        # If the main image is definitively broken, see if another supplied image works before refusing.
        for alt in images[1:3]:
            if probe_image(alt, timeout=timeout, require_ok=True, opener=opener, resolver=resolver) is None:
                return (f"{where}`image` {reason}. Pass a working photo URL first (for example `{alt}`).")
        hint = discover_photo(pages, timeout=timeout, opener=opener, resolver=resolver)
        extra = f" Found a working photo on the page: {hint} — verify it matches and pass it as `image`." if hint else ""
        return f"{where}`image` {reason}: pass a real, working photo URL from the {kind}'s page.{extra}"

    if len(items) == 1:
        res = check_one(items[0])
        return [res] if res else []
    out: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(items))) as pool:
        for res in pool.map(check_one, items):
            if res:
                out.append(res)
    return out
