"""Public-suffix-aware website identities used by the crawler frontier."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from functools import lru_cache

import tldextract

from app.discovery.url_safety import UrlSafetyError, normalize_url

# Do not fetch or refresh the PSL while the bot is running. tldextract ships a
# snapshot and this extractor is explicitly configured for offline operation.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)


@dataclass(frozen=True, slots=True)
class DomainIdentity:
    hostname: str
    registrable_domain: str
    origin: str


@lru_cache(maxsize=16_384)
def registrable_domain(hostname: str) -> str:
    """Return a stable eTLD+1 identity (or the normalized IP/unknown host)."""
    host = hostname.strip().rstrip(".").lower()
    if not host:
        return "unknown"
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    try:
        extracted = _EXTRACT(host)
    except Exception:
        return host
    if extracted.domain and extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}".lower()
    # Single-label names and public suffixes are still given a stable key;
    # URL safety rejects non-public names before any network request.
    return host


def domain_identity(url: str) -> DomainIdentity:
    """Build hostname, registrable-domain, and origin keys from an HTTP URL."""
    try:
        normalized = normalize_url(url)
    except UrlSafetyError:
        raise
    host = normalized.hostname.lower().rstrip(".")
    rendered_host = f"[{host}]" if ":" in host else host
    authority = rendered_host if normalized.port is None else f"{rendered_host}:{normalized.port}"
    return DomainIdentity(
        hostname=host,
        registrable_domain=registrable_domain(host),
        origin=f"{normalized.scheme}://{authority}",
    )


def identity_from_row(row: dict[str, object]) -> DomainIdentity:
    """Read an identity from stored fields, falling back to the page URL."""
    url = row.get("normalized_url") or row.get("url")
    if isinstance(url, str):
        try:
            return domain_identity(url)
        except UrlSafetyError:
            pass
    host = str(row.get("hostname") or row.get("domain") or "unknown").lower()
    return DomainIdentity(host, registrable_domain(host), f"https://{host}")
