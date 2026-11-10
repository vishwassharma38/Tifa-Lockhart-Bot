from __future__ import annotations

import pytest

from app.discovery.domains import domain_identity, registrable_domain
from app.discovery.url_safety import UrlSafetyError


def test_public_suffix_list_groups_subdomains_and_country_suffixes() -> None:
    assert registrable_domain("www.example.com") == "example.com"
    assert registrable_domain("artist.example.co.uk") == "example.co.uk"


def test_private_suffixes_separate_independent_creator_sites() -> None:
    assert registrable_domain("alice.github.io") == "alice.github.io"
    assert registrable_domain("bob.github.io") == "bob.github.io"


def test_domain_identity_preserves_origin_and_host_for_politeness() -> None:
    identity = domain_identity("https://gallery.example.com:8443/art/page")

    assert identity.hostname == "gallery.example.com"
    assert identity.registrable_domain == "example.com"
    assert identity.origin == "https://gallery.example.com:8443"


def test_ip_addresses_remain_individual_domain_identities() -> None:
    identity = domain_identity("https://[2606:4700:4700::1111]/page")

    assert identity.hostname == "2606:4700:4700::1111"
    assert identity.registrable_domain == "2606:4700:4700::1111"
    assert identity.origin == "https://[2606:4700:4700::1111]"


def test_malformed_urls_cannot_create_domain_identities() -> None:
    with pytest.raises(UrlSafetyError):
        domain_identity("not a URL")
