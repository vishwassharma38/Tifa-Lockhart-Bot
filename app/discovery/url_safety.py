"""Deterministic URL normalization and public-destination enforcement.

The crawler, Brave adapter and manual ``/save`` path all share this module.
Normalization is deliberately separate from destination validation: a URL may
be syntactically valid and still point at a private or internal address, so
every fetch must validate the destination again, including after redirects.
"""

from __future__ import annotations

import asyncio
import ipaddress
import posixpath
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_URL_LENGTH = 4096
DEFAULT_DNS_TIMEOUT_SECONDS = 5.0

_TRACKING_PARAMETERS = frozenset(
    {
        "_ga",
        "_gl",
        "dclid",
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "oly_anon_id",
        "oly_enc_id",
        "rb_click_id",
        "ref_src",
        "ref_url",
        "si",
        "ttclid",
        "twclid",
        "vero_id",
        "wickedid",
        "yclid",
    }
)
_TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_", "ns_", "sc_", "vero_")

_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".home.arpa",
    ".lan",
)
_BLOCKED_LITERALS = frozenset(
    {
        "169.254.169.254",
        "100.100.100.200",
        "fd00:ec2::254",
    }
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class UrlSafetyError(ValueError):
    """Raised when a URL is malformed or resolves to a non-public destination."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class NormalizedUrl:
    """Stable URL representation used for deduplication and fetching."""

    original: str
    normalized: str
    scheme: str
    hostname: str
    port: int | None
    path: str
    query: str


@dataclass(frozen=True, slots=True)
class ValidatedDestination:
    """A normalized URL plus the public addresses it resolved to."""

    url: NormalizedUrl
    addresses: tuple[str, ...]


def _normalize_hostname(hostname: str) -> str:
    candidate = hostname.strip().rstrip(".")
    if not candidate:
        raise UrlSafetyError("missing_hostname", "URL is missing a hostname")
    if "%" in candidate:
        raise UrlSafetyError("invalid_hostname", "Scoped IPv6 addresses are not permitted")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        return address.compressed

    try:
        ascii_host = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UrlSafetyError("invalid_hostname", "URL hostname is not valid IDNA") from exc

    if "." not in ascii_host or ascii_host.startswith(".") or ascii_host.endswith("."):
        raise UrlSafetyError("invalid_hostname", "URL must use a public DNS hostname")
    labels = ascii_host.split(".")
    for label in labels:
        if (
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(character in label for character in "_%\\")
        ):
            raise UrlSafetyError("invalid_hostname", "URL hostname contains an invalid label")
    return ascii_host


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    if "\\" in path:
        raise UrlSafetyError("invalid_url", "Backslashes are not permitted in URLs")
    had_trailing_slash = path.endswith("/")
    try:
        normalized = posixpath.normpath(path)
    except (TypeError, ValueError) as exc:
        raise UrlSafetyError("invalid_path", "URL path is invalid") from exc
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if had_trailing_slash and normalized not in {"/"} and not normalized.endswith("/"):
        normalized += "/"
    return normalized


def _is_tracking_parameter(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMETERS or lowered.startswith(_TRACKING_PREFIXES)


def _normalize_query(query: str) -> str:
    if not query:
        return ""
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=False)
    except ValueError as exc:
        raise UrlSafetyError("invalid_query", "URL query is malformed") from exc
    kept = [(key, value) for key, value in pairs if not _is_tracking_parameter(key)]
    # Sorting produces a stable deduplication key. Values are preserved exactly
    # (after standard percent-decoding and re-encoding), including image CDN
    # parameters that select a media variant.
    kept.sort(key=lambda item: (item[0], item[1]))
    return urlencode(kept, doseq=True)


def normalize_url(url: str) -> NormalizedUrl:
    """Return a deterministic HTTP(S) URL or raise :class:`UrlSafetyError`."""
    if not isinstance(url, str):
        raise UrlSafetyError("invalid_url", "URL must be a string")
    raw = url.strip()
    if not raw:
        raise UrlSafetyError("invalid_url", "URL must not be empty")
    if len(raw) > MAX_URL_LENGTH:
        raise UrlSafetyError("url_too_long", "URL exceeds the maximum supported length")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise UrlSafetyError("invalid_url", "URL contains control characters")
    if " " in raw:
        raise UrlSafetyError("invalid_url", "URL contains unescaped whitespace")

    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise UrlSafetyError("invalid_url", "URL is malformed") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UrlSafetyError("unsupported_scheme", "Only http and https URLs are accepted")
    if not parsed.netloc:
        raise UrlSafetyError("missing_hostname", "URL is missing a hostname")
    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise UrlSafetyError("credentials_in_url", "URLs containing credentials are rejected")
    if "\\" in parsed.netloc:
        raise UrlSafetyError("invalid_hostname", "Backslashes are not permitted in URLs")

    hostname = parsed.hostname
    if not hostname:
        raise UrlSafetyError("missing_hostname", "URL is missing a hostname")
    normalized_host = _normalize_hostname(hostname)

    try:
        port = parsed.port
    except ValueError as exc:
        raise UrlSafetyError("invalid_port", "URL port is invalid") from exc
    default_port = 443 if scheme == "https" else 80
    if port == default_port:
        port = None
    if port is not None and not 1 <= port <= 65535:
        raise UrlSafetyError("invalid_port", "URL port is outside the valid range")

    host_for_netloc = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    netloc = host_for_netloc if port is None else f"{host_for_netloc}:{port}"
    path = _normalize_path(parsed.path)
    query = _normalize_query(parsed.query)
    normalized = urlunsplit((scheme, netloc, path, query, ""))
    if len(normalized) > MAX_URL_LENGTH:
        raise UrlSafetyError("url_too_long", "Normalized URL exceeds the maximum length")

    return NormalizedUrl(
        original=raw,
        normalized=normalized,
        scheme=scheme,
        hostname=normalized_host,
        port=port,
        path=path,
        query=query,
    )


def is_public_ip(value: str) -> bool:
    """Return whether an address is globally routable and not a special range."""
    candidate = value.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    if not address.is_global:
        return False
    if isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network(
        "100.64.0.0/10"
    ):
        return False
    return True


def _blocked_hostname(hostname: str) -> bool:
    lowered = hostname.lower().rstrip(".")
    if lowered in _BLOCKED_HOSTNAMES or lowered in _BLOCKED_LITERALS:
        return True
    return any(lowered.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES)


def _check_literal_destination(url: NormalizedUrl) -> None:
    if _blocked_hostname(url.hostname):
        raise UrlSafetyError(
            "blocked_destination", "URL destination is local, internal, or a metadata endpoint"
        )
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        return
    if not is_public_ip(str(address)):
        raise UrlSafetyError("blocked_destination", "URL destination is not a public address")


def validate_url_literal(url: str) -> NormalizedUrl:
    """Normalize a URL and reject blocked names without performing DNS lookup."""
    normalized = normalize_url(url)
    _check_literal_destination(normalized)
    return normalized


def _default_port(url: NormalizedUrl) -> int:
    return url.port or (443 if url.scheme == "https" else 80)


async def resolve_public_addresses(
    hostname: str,
    port: int,
    *,
    timeout: float = DEFAULT_DNS_TIMEOUT_SECONDS,
) -> tuple[str, ...]:
    """Resolve every address for a hostname and reject if any is non-public."""
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            infos = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    hostname,
                    port,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                ),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise UrlSafetyError("dns_timeout", "DNS resolution timed out") from exc
        except OSError as exc:
            raise UrlSafetyError("dns_error", "DNS resolution failed") from exc
    else:
        infos = [
            (
                socket.AF_INET6 if literal.version == 6 else socket.AF_INET,
                0,
                0,
                "",
                (str(literal), port),
            )
        ]

    addresses: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        address = sockaddr[0]
        if not is_public_ip(address):
            raise UrlSafetyError(
                "blocked_destination",
                "Hostname resolved to a non-public address",
            )
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise UrlSafetyError("dns_error", "Hostname did not resolve")
    return tuple(addresses)


async def validate_public_url(
    url: str,
    *,
    timeout: float = DEFAULT_DNS_TIMEOUT_SECONDS,
) -> ValidatedDestination:
    """Normalize, resolve, and validate a URL immediately before a request."""
    normalized = validate_url_literal(url)
    addresses = await resolve_public_addresses(
        normalized.hostname,
        _default_port(normalized),
        timeout=timeout,
    )
    return ValidatedDestination(normalized, addresses)


class PublicResolver:
    """aiohttp resolver that only yields globally routable addresses.

    Resolving at connection time is what prevents a hostname from passing an
    earlier DNS check and then rebinding to a private address for the actual
    socket connection.
    """

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        lowered = host.lower().rstrip(".")
        if _blocked_hostname(lowered):
            raise OSError("Blocked non-public destination")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(
                    host,
                    port,
                    family=family,
                    type=socket.SOCK_STREAM,
                )
            except OSError:
                raise OSError("Destination hostname could not be resolved") from None
        else:
            infos = [(family, socket.SOCK_STREAM, 6, "", (str(literal), port))]

        results: list[dict[str, Any]] = []
        for address_family, _socktype, proto, _canonname, sockaddr in infos:
            address = sockaddr[0]
            if not is_public_ip(address):
                raise OSError("Blocked non-public destination")
            results.append(
                {
                    "hostname": host,
                    "host": address,
                    "port": port,
                    "family": address_family,
                    "proto": proto,
                    "flags": 0,
                }
            )
        if not results:
            raise OSError("Destination hostname did not resolve")
        return results

    async def close(self) -> None:
        return None


def is_redirect_status(status: int) -> bool:
    return status in _REDIRECT_STATUSES


def redirect_location(headers: Any) -> str | None:
    value = headers.get("Location") if headers is not None else None
    if value is None and headers is not None:
        value = headers.get("location")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
