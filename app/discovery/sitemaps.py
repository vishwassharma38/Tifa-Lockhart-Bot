"""Strictly bounded sitemap parsing for public page discovery."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from urllib.parse import urlsplit

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from app.discovery.url_safety import UrlSafetyError, normalize_url

MAX_SITEMAP_BYTES = 512_000
MAX_SITEMAP_FILES_PER_DOMAIN = 5
MAX_SITEMAP_URLS_PER_DOMAIN = 100
MAX_SITEMAP_LOC_LENGTH = 2_048


class SitemapError(ValueError):
    """A sitemap was malformed or exceeded its resource limits."""


@dataclass(frozen=True, slots=True)
class SitemapDocument:
    page_urls: tuple[str, ...]
    sitemap_urls: tuple[str, ...]
    is_index: bool


def _bounded_decompress(body: bytes) -> bytes:
    if len(body) > MAX_SITEMAP_BYTES:
        raise SitemapError("sitemap_body_too_large")
    if not body.startswith(b"\x1f\x8b"):
        return body
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        expanded = decompressor.decompress(body, MAX_SITEMAP_BYTES + 1)
        if len(expanded) > MAX_SITEMAP_BYTES or decompressor.unconsumed_tail:
            raise SitemapError("sitemap_decompression_limit")
        expanded += decompressor.flush(MAX_SITEMAP_BYTES + 1 - len(expanded))
    except zlib.error as exc:
        raise SitemapError("invalid_gzip_sitemap") from exc
    if len(expanded) > MAX_SITEMAP_BYTES or not decompressor.eof:
        raise SitemapError("sitemap_decompression_limit")
    return expanded


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(
    body: bytes,
    sitemap_url: str,
    *,
    max_urls: int = MAX_SITEMAP_URLS_PER_DOMAIN,
) -> SitemapDocument:
    """Parse a urlset or sitemapindex without external entities or recursion."""
    try:
        normalized_sitemap = normalize_url(sitemap_url)
        content = _bounded_decompress(body)
        root = ElementTree.fromstring(content)
    except (UrlSafetyError, DefusedXmlException, ElementTree.ParseError, SitemapError) as exc:
        if isinstance(exc, SitemapError):
            raise
        raise SitemapError("invalid_sitemap_xml") from exc

    root_name = _local_name(root.tag)
    if root_name not in {"urlset", "sitemapindex"}:
        raise SitemapError("unsupported_sitemap_root")
    is_index = root_name == "sitemapindex"
    expected_host = normalized_sitemap.hostname
    page_urls: list[str] = []
    sitemap_urls: list[str] = []
    seen: set[str] = set()
    expected_item = "sitemap" if is_index else "url"

    for item in root:
        if _local_name(item.tag) != expected_item:
            continue
        loc = next((child.text for child in item if _local_name(child.tag) == "loc"), None)
        if not isinstance(loc, str):
            continue
        candidate = loc.strip()
        if not candidate or len(candidate) >= MAX_SITEMAP_LOC_LENGTH:
            continue
        try:
            normalized = normalize_url(candidate).normalized
            destination_host = urlsplit(normalized).hostname
        except UrlSafetyError:
            continue
        # Sitemap protocol locations stay on the sitemap host. This also
        # prevents a remote sitemap from injecting unrelated hosts into the
        # cross-domain frontier.
        if destination_host != expected_host or normalized in seen:
            continue
        seen.add(normalized)
        destination = sitemap_urls if is_index else page_urls
        destination.append(normalized)
        if len(destination) >= max(0, max_urls):
            break

    return SitemapDocument(tuple(page_urls), tuple(sitemap_urls), is_index)
