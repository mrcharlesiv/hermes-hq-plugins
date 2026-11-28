"""Card image checks, metadata extraction and live probe hints (0.9.93)."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

HERE = Path(__file__).parent


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


card_images = _load("hermes_hq_card_images", "card_images.py")
shopping = _load("hermes_hq_shopping", "shopping.py")
travel = _load("hermes_hq_travel", "travel.py")

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, content_type: str = "image/jpeg", url: str = "https://example.com"):
        self._buf = io.BytesIO(body)
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.url = url

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_bad_image_reason_catches_non_photos():
    assert card_images.bad_image_reason("https://example.com/p/item-1", "https://example.com/p/item-1/")
    assert card_images.bad_image_reason("https://cdn.example.com/photo.svg")
    assert card_images.bad_image_reason("https://cdn.example.com/img?format=svg")
    assert card_images.bad_image_reason("https://example.com/favicon.png")
    assert card_images.bad_image_reason("https://cdn.example.com/assets/Instacart_Share_Banner_3.jpg")
    assert card_images.bad_image_reason("https://cdn.example.com/site-logo.png")
    assert card_images.bad_image_reason("https://cdn.example.com/brand_logo_dark.webp")
    assert card_images.bad_image_reason("https://cdn.example.com/products/logo-tshirt-navy.jpg") is None
    assert card_images.bad_image_reason("https://cdn.example.com/products/12345.jpg", "https://example.com/p/item-1") is None
    assert card_images.bad_image_reason("https://scene.example.com/is/image/Store/GUEST_1234") is None


def test_public_ip_and_https_policy_blocks_private_and_tailscale():
    assert card_images.is_public_ip("93.184.216.34")
    for bad in ("127.0.0.1", "10.0.0.1", "192.168.1.5", "172.16.0.1", "100.64.0.1", "100.100.100.100",
                "169.254.169.254", "::1", "fc00::1", "fd7a:115c:a1e0::1", "fe80::1%en0", "::ffff:127.0.0.1"):
        assert not card_images.is_public_ip(bad), bad
    assert not card_images.is_public_https("http://example.com/a.jpg", resolver=lambda _: ["93.184.216.34"])
    assert not card_images.is_public_https("https://box.tail1234.ts.net/a.jpg", resolver=lambda _: ["93.184.216.34"])
    assert not card_images.is_public_https("https://rebind.example.com/a.jpg",
                                           resolver=lambda _: ["93.184.216.34", "10.0.0.5"])
    assert card_images.is_public_https("https://cdn.example.com/a.jpg", resolver=lambda _: ["93.184.216.34"])


def test_extract_page_images_prefers_json_ld_product_then_og_and_skips_banners():
    html = """
    <html><head>
      <meta property="og:image" content="https://cdn.example.com/assets/Instacart_Share_Banner_3.jpg">
      <meta property="twitter:image" content="/images/fallback.webp">
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"Product","name":"Bottle","image":["https://cdn.example.com/prod-main.jpg"]}
      </script>
    </head></html>
    """
    got = card_images.extract_page_images(html, "https://store.example.com/p/bottle")
    assert got == ["https://cdn.example.com/prod-main.jpg", "https://store.example.com/images/fallback.webp"]


def test_probe_image_refuses_404_and_html_but_allows_valid_and_403():
    def opener(url, accept, timeout):
        if "404" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if "403" in url:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        if "html" in url:
            return FakeResponse(b"<!doctype html><html></html>", content_type="text/html; charset=utf-8")
        if "svg" in url:
            return FakeResponse(b"<svg></svg>", content_type="image/svg+xml")
        if "octet" in url:
            return FakeResponse(PNG_BYTES, content_type="application/octet-stream")
        return FakeResponse(JPEG_BYTES, content_type="image/jpeg")

    assert card_images.probe_image("https://cdn.example.com/ok.jpg", opener=opener) is None
    assert card_images.probe_image("https://cdn.example.com/octet.png", opener=opener, require_ok=True) is None
    assert card_images.probe_image("https://cdn.example.com/403.jpg", opener=opener) is None
    assert card_images.probe_image("https://cdn.example.com/403.jpg", opener=opener, require_ok=True) is not None
    assert "404" in (card_images.probe_image("https://cdn.example.com/404.jpg", opener=opener) or "")
    assert "text/html" in (card_images.probe_image("https://cdn.example.com/html", opener=opener) or "")
    assert "SVG" in (card_images.probe_image("https://cdn.example.com/svg", opener=opener) or "")


def test_verify_and_hint_discovers_offer_photo_when_product_image_is_missing():
    responses = {
        "https://shop1.example.com/p/drink": FakeResponse(
            b'<meta property="og:image" content="https://cdn.example.com/Instacart_Share_Banner_3.jpg">',
            content_type="text/html",
            url="https://shop1.example.com/p/drink",
        ),
        "https://shop2.example.com/p/drink": FakeResponse(
            b'<meta property="og:image" content="https://cdn.example.com/drinks/real-photo.jpg">',
            content_type="text/html",
            url="https://shop2.example.com/p/drink",
        ),
        "https://cdn.example.com/drinks/real-photo.jpg": FakeResponse(JPEG_BYTES, content_type="image/jpeg"),
    }

    def opener(url, accept, timeout):
        if url not in responses:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return responses[url]

    args = {
        "products": [{
            "title": "Protein Drink 4-pack",
            "store": "Shop One",
            "url": "https://shop1.example.com/p/drink",
            "price": 10.49,
            "offers": [{"store": "Shop Two", "url": "https://shop2.example.com/p/drink", "price": 10.99}],
        }]
    }
    reply = json.loads(shopping.show(
        args, "s1",
        probe=lambda items, kind="product": card_images.verify_and_hint(items, kind=kind, opener=opener),
    ))
    assert reply["status"] == "failed"
    assert "https://cdn.example.com/drinks/real-photo.jpg" in reply["error"]


def test_verify_and_hint_refuses_broken_404_supplied_image_for_hotel():
    def opener(url, accept, timeout):
        if "broken" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if "hotel-page" in url:
            return FakeResponse(
                b'<meta property="og:image" content="https://cdn.example.com/hotels/real.jpg">',
                content_type="text/html",
                url=url,
            )
        return FakeResponse(JPEG_BYTES, content_type="image/jpeg")

    args = {
        "hotels": [{
            "name": "Example Hotel",
            "url": "https://hotels.example.com/hotel-page",
            "price_per_night": 190,
            "image": "https://cdn.example.com/broken.jpg",
        }]
    }
    reply = json.loads(travel.show_hotels(
        args,
        probe=lambda items, kind="hotel": card_images.verify_and_hint(items, kind=kind, opener=opener),
    ))
    assert reply["status"] == "failed"
    assert "404" in reply["error"]
    assert "https://cdn.example.com/hotels/real.jpg" in reply["error"]
