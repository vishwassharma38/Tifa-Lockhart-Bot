from __future__ import annotations

import pytest

from app.discovery.extractor import (
    clean_text,
    extract_http_link_header,
    extract_page,
    is_direct_media_url,
)

PAGE_HTML = """
<!doctype html>
<html>
  <head>
    <title>  Tifa   Lockhart page </title>
    <meta name="description" content="A page about Tifa Lockhart.">
    <meta property="og:title" content="OG Tifa Lockhart">
    <meta property="og:description" content="OG description for Tifa.">
    <meta name="twitter:title" content="Twitter title">
    <meta name="twitter:image" content="/twitter-preview.jpg">
    <meta name="author" content="Example Artist">
    <meta name="keywords" content="Tifa, Final Fantasy VII">
    <meta property="og:image" content="/social-preview.png">
    <link rel="canonical" href="/canonical?utm_source=feed">
    <style>.hidden { color: red; }</style>
    <script>var secret = "script text";</script>
  </head>
  <body>
    <main>
      <h1>Tifa</h1>
      <p>Visible body #Hashtag</p>
      <a href="/next?utm_source=feed">Next page</a>
      <a href="javascript:alert(1)">Bad link</a>
      <a href="mailto:test@example.com">Email</a>
      <a href="/next?utm_source=feed#again">Duplicate next</a>
      <a href="/original.webp">Download original</a>
      <a href="/clip.mp4">Video file</a>
      <img src="/thumb.jpg" alt="Thumbnail">
      <img data-original="/full.png" src="/thumb2.jpg" alt="Full resolution">
      <img srcset="/small.jpg 200w, /large.jpg 1200w" src="/fallback.jpg" alt="Responsive">
      <img srcset="/one.jpg 1x, /two.jpg 2x" src="/fallback2.jpg" alt="Density">
      <img src="/site-logo.png" alt="Site logo">
      <a rel="tag" href="/tags/tifa">Tifa tag</a>
    </main>
  </body>
</html>
"""


def test_clean_text_collapses_whitespace_and_applies_a_limit() -> None:
    assert clean_text("  one\n\t two   three  ") == "one two three"
    assert clean_text(None) == ""
    assert clean_text("abcdef", limit=3) == "abc"


def test_extract_page_collects_metadata_text_and_canonical_url() -> None:
    page = extract_page(PAGE_HTML, "https://example.com/gallery/page.html")

    assert page.title == "Tifa Lockhart page"
    assert page.description == "A page about Tifa Lockhart."
    assert page.og_title == "OG Tifa Lockhart"
    assert page.og_description == "OG description for Tifa."
    assert page.twitter_title == "Twitter title"
    assert page.author == "Example Artist"
    assert page.canonical_url == "https://example.com/canonical"
    assert page.classification_title == "OG Tifa Lockhart"
    assert page.classification_description == "OG description for Tifa."
    assert "Visible body #Hashtag" in page.text
    assert "script text" not in page.text
    assert "color: red" not in page.text
    assert "Hashtag" in page.tags
    assert "Tifa" in page.tags
    assert "Final Fantasy VII" in page.tags
    assert "Tifa tag" in page.tags


def test_extract_page_normalizes_and_deduplicates_links() -> None:
    page = extract_page(PAGE_HTML, "https://example.com/gallery/page.html")
    links = [(link.url, link.text) for link in page.links]

    assert ("https://example.com/next", "Next page") in links
    assert sum(link.url == "https://example.com/next" for link in page.links) == 1
    assert all(not link.url.startswith(("javascript:", "mailto:")) for link in page.links)
    assert ("https://example.com/original.webp", "Download original") in links
    assert ("https://example.com/clip.mp4", "Video file") in links


def test_extract_page_classifies_original_and_preview_media() -> None:
    page = extract_page(PAGE_HTML, "https://example.com/gallery/page.html")
    by_url = {candidate.url: candidate for candidate in page.media}

    full = by_url["https://example.com/full.png"]
    assert full.original is True
    assert full.is_preview is False
    assert full.source == "image"

    responsive = by_url["https://example.com/large.jpg"]
    assert responsive.original is True
    assert responsive.is_preview is False
    assert responsive.source == "srcset"
    assert responsive.width == 1200

    density = by_url["https://example.com/two.jpg"]
    assert density.original is True
    assert density.is_preview is False
    assert density.width is None

    thumbnail = by_url["https://example.com/thumb.jpg"]
    assert thumbnail.original is False
    assert thumbnail.is_preview is True

    logo = by_url["https://example.com/site-logo.png"]
    assert logo.original is False
    assert logo.is_preview is True

    og_image = by_url["https://example.com/social-preview.png"]
    assert og_image.original is False
    assert og_image.is_preview is True
    assert og_image.source == "og_image"

    twitter_image = by_url["https://example.com/twitter-preview.jpg"]
    assert twitter_image.original is False
    assert twitter_image.is_preview is True
    assert twitter_image.source == "twitter_image"

    anchor = by_url["https://example.com/original.webp"]
    assert anchor.original is True
    assert anchor.source == "direct_anchor"


def test_extract_page_original_media_excludes_all_previews() -> None:
    page = extract_page(PAGE_HTML, "https://example.com/gallery/page.html")
    original_urls = {candidate.url for candidate in page.original_media}

    assert "https://example.com/full.png" in original_urls
    assert "https://example.com/large.jpg" in original_urls
    assert "https://example.com/two.jpg" in original_urls
    assert "https://example.com/original.webp" in original_urls
    assert "https://example.com/clip.mp4" in original_urls
    assert "https://example.com/thumb.jpg" not in original_urls
    assert "https://example.com/social-preview.png" not in original_urls


def test_extract_page_metadata_is_bounded_and_truthful() -> None:
    page = extract_page(PAGE_HTML, "https://example.com/gallery/page.html")
    metadata = page.to_metadata()

    assert metadata["canonical_url"] == "https://example.com/canonical"
    assert metadata["link_count"] == len(page.links)
    assert metadata["media_count"] == len(page.media)
    assert metadata["original_media_count"] == len(page.original_media)
    assert len(metadata["text_excerpt"]) <= 4000


def test_extract_page_ignores_unsafe_canonical_links() -> None:
    html = """
    <html><head>
      <link rel="canonical" href="javascript:alert(1)">
      <link rel="canonical" href="http://127.0.0.1/private">
    </head><body><p>Tifa Lockhart</p></body></html>
    """

    page = extract_page(html, "https://example.com/page")

    assert page.canonical_url is None


def test_extract_page_resolves_base_area_and_meaningful_link_relations() -> None:
    html = """
    <html><head>
      <base href="https://artist.example/gallery/">
      <link rel="next" href="page-2">
      <link rel="canonical" href="https://unrelated.example/canonical">
    </head><body>
      <area href="artwork" alt="Tifa artwork">
      <a href="portfolio">Artist portfolio</a>
    </body></html>
    """

    page = extract_page(html, "https://artist.example/old/page")
    links = {link.url: link for link in page.links}

    assert "https://artist.example/gallery/page-2" in links
    assert links["https://artist.example/gallery/page-2"].kind == "next"
    assert "https://artist.example/gallery/artwork" in links
    assert links["https://artist.example/gallery/artwork"].kind == "area"
    assert "https://artist.example/gallery/portfolio" in links
    assert "https://unrelated.example/canonical" not in links


def test_extract_page_drops_action_and_infinite_navigation_routes() -> None:
    page = extract_page(
        """<html><body>
        <a href="/login">Login</a>
        <a href="/search?q=tifa">Search</a>
        <a href="/a/b/a/b/a/b/a/b/a">Repeating path</a>
        <a href="/gallery/tifa">Tifa gallery</a>
        </body></html>""",
        "https://example.com/",
    )

    assert [link.url for link in page.links] == ["https://example.com/gallery/tifa"]


def test_http_link_header_parses_useful_relations_and_skips_canonical() -> None:
    links = extract_http_link_header(
        '<https://example.com/page-2>; rel="next", '
        '<https://example.com/artist>; rel="author"; title="Artist portfolio", '
        '<https://canonical.example/page>; rel="canonical"',
        "https://example.com/gallery/page-1",
    )

    assert [(link.url, link.kind) for link in links] == [
        ("https://example.com/page-2", "next"),
        ("https://example.com/artist", "author"),
    ]


def test_extract_page_without_metadata_still_returns_text() -> None:
    page = extract_page("<html><body><p>Only body text</p></body></html>", "https://example.com/")

    assert page.title is None
    assert page.canonical_url is None
    assert page.text == "Only body text"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/file.jpg", True),
        ("https://example.com/file.WEBP", True),
        ("https://example.com/file.mp4", True),
        ("https://example.com/file", False),
        ("https://example.com/page.html", False),
        ("not a url", False),
    ],
)
def test_is_direct_media_url_uses_the_shared_extension_rules(url: str, expected: bool) -> None:
    assert is_direct_media_url(url) is expected


def test_extract_page_requires_a_safe_final_url() -> None:
    with pytest.raises(ValueError):
        extract_page("<html></html>", "not a url")
