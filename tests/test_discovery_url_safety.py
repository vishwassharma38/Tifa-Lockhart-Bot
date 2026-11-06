from __future__ import annotations

import asyncio
import socket

import pytest

from app.discovery.url_safety import (
    PublicResolver,
    UrlSafetyError,
    is_public_ip,
    is_redirect_status,
    normalize_url,
    redirect_location,
    resolve_public_addresses,
    validate_public_url,
    validate_url_literal,
)


def test_normalize_url_produces_a_stable_deduplication_key() -> None:
    normalized = normalize_url("HTTPS://Example.COM:443/a/../b/?utm_source=spam&b=2&a=1#section")

    assert normalized.normalized == "https://example.com/b/?a=1&b=2"
    assert normalized.scheme == "https"
    assert normalized.hostname == "example.com"
    assert normalized.port is None
    assert normalized.path == "/b/"
    assert normalized.query == "a=1&b=2"


def test_normalize_url_preserves_meaningful_repeated_and_blank_query_values() -> None:
    normalized = normalize_url("https://cdn.example/image?size=large&size=small&empty=")

    assert normalized.query == "empty=&size=large&size=small"


def test_normalize_url_removes_tracking_parameters_case_insensitively() -> None:
    normalized = normalize_url(
        "https://example.com/page?UTM_Medium=social&fbclid=x&keep=1&si=tracking"
    )

    assert normalized.normalized == "https://example.com/page?keep=1"


def test_normalize_url_handles_idna_hosts_and_trailing_dots() -> None:
    normalized = normalize_url("https://täfa.example./path")

    assert normalized.hostname.startswith("xn--")
    assert normalized.normalized.startswith("https://xn--")


def test_normalize_url_keeps_explicit_non_default_ports() -> None:
    normalized = normalize_url("http://example.com:8080/path")

    assert normalized.port == 8080
    assert normalized.normalized == "http://example.com:8080/path"


def test_normalize_url_keeps_ipv6_literal_brackets() -> None:
    normalized = normalize_url("https://[2606:4700:4700::1111]/dns-query")

    assert normalized.hostname == "2606:4700:4700::1111"
    assert normalized.normalized.startswith("https://[2606:4700:4700::1111]/")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("", "invalid_url"),
        ("   ", "invalid_url"),
        ("ftp://example.com/file", "unsupported_scheme"),
        ("file:///etc/passwd", "unsupported_scheme"),
        ("javascript:alert(1)", "unsupported_scheme"),
        ("https://user:password@example.com/", "credentials_in_url"),
        ("https://example.com/a b", "invalid_url"),
        ("https://example.com/\npath", "invalid_url"),
        ("https://example.com\\path", "invalid_hostname"),
        ("https://localhost/", "invalid_hostname"),
        ("https://example.com:99999/", "invalid_port"),
        ("https://example.com:notaport/", "invalid_port"),
        ("https://exa_mple.com/", "invalid_hostname"),
    ],
)
def test_normalize_url_rejects_unsafe_or_malformed_inputs(url: str, reason: str) -> None:
    with pytest.raises(UrlSafetyError) as captured:
        normalize_url(url)

    assert captured.value.reason == reason


def test_normalize_url_rejects_urls_beyond_the_hard_limit() -> None:
    with pytest.raises(UrlSafetyError, match="maximum supported length"):
        normalize_url("https://example.com/" + "a" * 5000)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.1.1",
        "100.64.0.1",
        "0.0.0.0",
        "::1",
        "fd00::1",
        "fe80::1",
        "192.0.2.1",
    ],
)
def test_non_public_addresses_are_rejected(address: str) -> None:
    assert is_public_ip(address) is False


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_are_accepted(address: str) -> None:
    assert is_public_ip(address) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://localhost.localdomain/",
        "http://metadata.google.internal/",
        "http://instance-data.ec2.internal/",
        "http://printer.local/",
        "http://router.home.arpa/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.100.100.200/",
        "http://10.0.0.5/",
    ],
)
def test_validate_url_literal_blocks_local_and_metadata_destinations(url: str) -> None:
    with pytest.raises(UrlSafetyError) as captured:
        validate_url_literal(url)

    assert captured.value.reason in {"blocked_destination", "invalid_hostname"}


def test_validate_url_literal_accepts_a_public_literal() -> None:
    normalized = validate_url_literal("https://8.8.8.8/dns")

    assert normalized.normalized == "https://8.8.8.8/dns"


def _fake_loop(infos: list[tuple[object, ...]]) -> object:
    class Loop:
        async def getaddrinfo(self, *_args: object, **_kwargs: object):
            return infos

    return Loop()


def _address_info(*addresses: str, family: int = socket.AF_INET) -> list[tuple[object, ...]]:
    return [(family, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]


async def test_resolve_public_addresses_returns_every_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        asyncio,
        "get_running_loop",
        lambda: _fake_loop(_address_info("8.8.8.8", "1.1.1.1")),
    )

    addresses = await resolve_public_addresses("example.com", 443)

    assert addresses == ("8.8.8.8", "1.1.1.1")


async def test_resolve_public_addresses_rejects_a_single_private_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        asyncio,
        "get_running_loop",
        lambda: _fake_loop(_address_info("8.8.8.8", "10.0.0.10")),
    )

    with pytest.raises(UrlSafetyError, match="non-public"):
        await resolve_public_addresses("rebind.example", 443)


async def test_resolve_public_addresses_reports_dns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Loop:
        async def getaddrinfo(self, *_args: object, **_kwargs: object):
            raise OSError("dns failed")

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: Loop())

    with pytest.raises(UrlSafetyError) as captured:
        await resolve_public_addresses("example.com", 443)

    assert captured.value.reason == "dns_error"


async def test_validate_public_url_combines_normalization_and_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        asyncio,
        "get_running_loop",
        lambda: _fake_loop(_address_info("8.8.8.8")),
    )

    destination = await validate_public_url("HTTPS://Example.com:443/path#fragment")

    assert destination.url.normalized == "https://example.com/path"
    assert destination.addresses == ("8.8.8.8",)


async def test_public_resolver_rejects_blocked_hostnames() -> None:
    with pytest.raises(OSError, match="Blocked"):
        await PublicResolver().resolve("metadata.google.internal", 80)


async def test_public_resolver_rejects_private_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        asyncio,
        "get_running_loop",
        lambda: _fake_loop(_address_info("127.0.0.1")),
    )

    with pytest.raises(OSError, match="Blocked"):
        await PublicResolver().resolve("rebind.example", 80)


async def test_public_resolver_returns_aiohttp_compatible_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        asyncio,
        "get_running_loop",
        lambda: _fake_loop(_address_info("8.8.8.8")),
    )

    records = await PublicResolver().resolve("example.com", 443)

    assert records == [
        {
            "hostname": "example.com",
            "host": "8.8.8.8",
            "port": 443,
            "family": socket.AF_INET,
            "proto": 6,
            "flags": 0,
        }
    ]


def test_redirect_helpers_cover_common_variants() -> None:
    assert is_redirect_status(301) is True
    assert is_redirect_status(303) is True
    assert is_redirect_status(308) is True
    assert is_redirect_status(200) is False
    assert redirect_location({"location": "/next"}) == "/next"
    assert redirect_location({"Location": " https://example.com/next "}) == (
        "https://example.com/next"
    )
    assert redirect_location({}) is None
    assert redirect_location(None) is None
