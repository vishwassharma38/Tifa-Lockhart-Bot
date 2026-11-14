from __future__ import annotations

import gzip

import pytest

from app.discovery.sitemaps import SitemapError, parse_sitemap

URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/tifa-gallery</loc></url>
  <url><loc>https://other.example/tifa</loc></url>
  <url><loc>https://example.com/tifa-gallery</loc></url>
</urlset>"""


def test_parse_sitemap_urlset_deduplicates_and_rejects_other_hosts() -> None:
    result = parse_sitemap(URLSET, "https://example.com/sitemap.xml")

    assert result.is_index is False
    assert result.page_urls == ("https://example.com/tifa-gallery",)
    assert result.sitemap_urls == ()


def test_parse_sitemap_index_is_bounded_and_does_not_fetch_recursively() -> None:
    body = b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://example.com/sitemap-art.xml</loc></sitemap>
      <sitemap><loc>https://example.com/sitemap-creators.xml</loc></sitemap>
    </sitemapindex>"""

    result = parse_sitemap(body, "https://example.com/sitemap.xml", max_urls=1)

    assert result.is_index is True
    assert result.page_urls == ()
    assert result.sitemap_urls == ("https://example.com/sitemap-art.xml",)


def test_parse_sitemap_supports_bounded_gzip() -> None:
    result = parse_sitemap(gzip.compress(URLSET), "https://example.com/sitemap.xml.gz")

    assert result.page_urls == ("https://example.com/tifa-gallery",)


def test_parse_sitemap_rejects_entity_expansion_and_oversized_content() -> None:
    with pytest.raises(SitemapError):
        parse_sitemap(
            b'<!DOCTYPE x [<!ENTITY e "boom">]><urlset><url><loc>&e;</loc></url></urlset>',
            "https://example.com/sitemap.xml",
        )
    with pytest.raises(SitemapError, match="sitemap_body_too_large"):
        parse_sitemap(b"x" * 512_001, "https://example.com/sitemap.xml")
