"""A 1Password login fills on its whole site (site_match.py), against Hermes's real vault tool."""
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("hermes_hq_push_site_match_test", Path(__file__).with_name("site_match.py"))
site_match = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site_match)


@pytest.mark.parametrize("saved, page", [
    ("https://example.com", "https://example.com"),
    ("https://example.com", "https://www.example.com"),
    ("https://www.example.com", "https://example.com"),
    ("https://example.com", "https://account.example.com"),
    ("https://account.example.com", "https://example.com"),         # a parent holds the saved host's DNS already
    ("https://account.example.com", "https://www.example.com"),
    ("https://www.example.com", "https://identity.example.com"),    # www. is the site's own name
    ("https://a.b.example.com", "https://b.example.com"),
    ("https://shop.example.co.uk", "https://www.example.co.uk"),
    ("https://example.com:8443", "https://login.example.com:8443"),
])
def test_the_same_site_fills(saved, page):
    assert site_match.covers(saved, page)


@pytest.mark.parametrize("saved, page", [
    ("https://example.com", "http://example.com"),                   # never an https login on http
    ("https://example.com", "https://example.com:8443"),
    ("https://example.com", "https://example.org"),
    ("https://example.com", "https://notexample.com"),
    ("https://example.com", "https://example.com.evil.test"),
    ("https://example.co.uk", "https://other.co.uk"),                # co.uk is a suffix, not a site
    ("https://alice.github.io", "https://mallory.github.io"),         # shared hosting: each name is someone else's
    ("https://shop.myshopify.com", "https://other.myshopify.com"),
    ("https://192.168.1.10", "https://192.168.1.11"),
    ("http://localhost:3000", "http://other:3000"),
    # [issue #11] two names side by side can't be told from two tenants without a list, so none match, listed or not
    ("https://alice.duckdns.org", "https://mallory.duckdns.org"),
    ("https://a.webflow.io", "https://b.webflow.io"),
    ("https://a.uk.com", "https://b.uk.com"),
    ("https://alice.unlisted-host.example", "https://mallory.unlisted-host.example"),   # never on any list
    ("https://www.alice.unlisted-host.example", "https://mallory.unlisted-host.example"),
    ("https://app.example.com", "https://login.example.com"),       # a sibling keeps Hermes's exact refusal
    ("https://shop.example.co.uk", "https://other.co.uk"),
    # a shared host's own top name never reaches a tenant below it, nor a tenant its top name
    ("https://www.duckdns.org", "https://mallory.duckdns.org"),
    ("https://duckdns.org", "https://mallory.duckdns.org"),
    ("https://github.io", "https://mallory.github.io"),
    ("https://alice.github.io", "https://github.io"),
    ("https://example.com", "https://com"),
    ("https://www.example.com", "https://www.example.com.evil.test"),
])
def test_another_site_does_not(saved, page):
    assert not site_match.covers(saved, page)


def test_no_list_decides_between_two_unlisted_tenants(monkeypatch):
    """A stale or empty shared-hosting list can't open one tenant to another: that rests on the names alone."""
    monkeypatch.setattr(site_match, "_SHARED", set())
    assert not site_match.covers("https://alice.duckdns.org", "https://mallory.duckdns.org")
    assert not site_match.covers("https://alice.github.io", "https://mallory.github.io")
    assert site_match.covers("https://example.com", "https://account.example.com")


def test_the_site_of_a_host():
    assert site_match.site("account.example.com") == "example.com"
    assert site_match.site("a.b.example.com.au") == "example.com.au"
    assert site_match.site("co.uk") is None
    assert site_match.site("x.vercel.app") is None
    assert site_match.site("10.0.0.1") is None


def _meta(handle, origins):
    from agent.vault_store import VaultItemMeta
    return VaultItemMeta(id=handle, kind="login", label="Example", origin=origins[0], created_at="",
                         allowed_origins=tuple(origins))


def test_hermes_fills_a_1password_login_on_its_site_and_nothing_else_changes(monkeypatch):
    import tools.browser_vault_tool as tool
    hermes_own = tool._bound_page_origin
    while getattr(hermes_own, site_match.MARK, False):   # the add-on loaded by an earlier test already installed it
        hermes_own = hermes_own.__wrapped__
    monkeypatch.setattr(tool, "_bound_page_origin", hermes_own)   # undone after the test
    page = {"origin": "https://www.example.com"}
    monkeypatch.setattr(tool, "_current_page_origin", lambda task_id: page["origin"])
    monkeypatch.setattr(tool, "_focus_bound_origin", lambda task_id, origin, kind: None)
    one_password = _meta("op:abc", ["https://example.com"])
    own_vault = _meta("hv:abc", ["https://example.com"])
    before = tool._bound_page_origin("t", one_password, "login")
    assert isinstance(before, dict) and before["error_type"] == "origin_mismatch"     # Hermes alone: exact only

    assert site_match.install() == "installed" and site_match.install() == "already"
    assert tool._bound_page_origin("t", one_password, "login") == "https://www.example.com"
    assert tool._bound_page_origin("t", one_password, "otp") == "https://www.example.com"
    assert tool._bound_page_origin("t", own_vault, "login")["error_type"] == "origin_mismatch"   # other vaults: as before
    page["origin"] = "https://example.org"
    refused = tool._bound_page_origin("t", one_password, "login")
    assert refused["error_type"] == "origin_mismatch" and "example.org" in json.dumps(refused)
    page["origin"] = "http://www.example.com"
    assert tool._bound_page_origin("t", one_password, "login")["error_type"] == "origin_mismatch"
    page["origin"] = "https://example.com"
    assert tool._bound_page_origin("t", one_password, "login") == "https://example.com"   # exact still fills
