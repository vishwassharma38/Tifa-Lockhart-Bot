"""Bounded HTML metadata, link, and media-candidate extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.discovery.url_safety import UrlSafetyError, normalize_url
from app.media.validation import normalize_extension

_TEXT_LIMIT = 20_000
_STORED_TEXT_LIMIT = 4_000
_MAX_LINKS = 1_000
_MAX_MEDIA = 200
_HASHTAG_PATTERN = re.compile(r"(?<![\w#])#([A-Za-z0-9_]{2,50})")
_WHITESPACE = re.compile(r"\s+")
_NON_HTTP_PREFIXES = ("javascript:", "mailto:", "tel:", "data:", "about:", "blob:")
_UNSUITABLE_IMAGE_HINTS = ("sprite", "icon", "logo", "avatar", "emoji", "badge", "pixel")


@dataclass(frozen=True, slots=True)
class ExtractedLink:
    url: str
    text: str
    original_url: str | None = None

    def to_metadata(self) -> dict[str, str]:
        return {"url": self.url, "text": self.text[:300]}


@dataclass(frozen=True, slots=True)
class MediaCandidate:
    """A media URL found in ordinary HTML.

    ``original`` means the page exposed a plausible original/full-resolution
    file. Social previews and generic thumbnails are deliberately marked
    ``is_preview`` and are never auto-acquired.
    """

    url: str
    source: str
    original: bool
    is_preview: bool
    width: int | None = None
    alt: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "source": self.source,
            "original": self.original,
            "is_preview": self.is_preview,
            "width": self.width,
            "alt": self.alt,
        }


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    final_url: str
    title: str | None
    description: str | None
    canonical_url: str | None
    og_title: str | None
    og_description: str | None
    twitter_title: str | None
    twitter_description: str | None
    author: str | None
    tags: tuple[str, ...]
    text: str
    links: tuple[ExtractedLink, ...] = ()
    media: tuple[MediaCandidate, ...] = field(default_factory=tuple)

    @property
    def classification_title(self) -> str:
        return self.og_title or self.twitter_title or self.title or ""

    @property
    def classification_description(self) -> str:
        return self.og_description or self.twitter_description or self.description or ""

    @property
    def original_media(self) -> tuple[MediaCandidate, ...]:
        return tuple(
            candidate for candidate in self.media if candidate.original and not candidate.is_preview
        )

    def to_metadata(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "canonical_url": self.canonical_url,
            "og_title": self.og_title,
            "og_description": self.og_description,
            "twitter_title": self.twitter_title,
            "twitter_description": self.twitter_description,
            "author": self.author,
            "tags": list(self.tags),
            "text_excerpt": self.text[:_STORED_TEXT_LIMIT],
            "link_count": len(self.links),
            "media_count": len(self.media),
            "original_media_count": len(self.original_media),
        }


def clean_text(value: str | None, *, limit: int = _TEXT_LIMIT) -> str:
    if not value:
        return ""
    return _WHITESPACE.sub(" ", value).strip()[:limit]


def _meta_values(soup: BeautifulSoup) -> dict[str, str]:
    values: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        content = tag.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        key = tag.get("property") or tag.get("name")
        if not isinstance(key, str):
            continue
        lowered = key.strip().lower()
        if lowered and lowered not in values:
            values[lowered] = clean_text(content, limit=2_000)
    return values


def _title(soup: BeautifulSoup) -> str | None:
    if soup.title and soup.title.string:
        value = clean_text(soup.title.string, limit=1_000)
        return value or None
    return None


def _canonical_url(soup: BeautifulSoup, base_url: str) -> str | None:
    for tag in soup.find_all("link"):
        rel = tag.get("rel")
        if isinstance(rel, str):
            relations = {rel.lower()}
        elif isinstance(rel, list):
            relations = {str(value).lower() for value in rel}
        else:
            continue
        if "canonical" not in relations:
            continue
        href = tag.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        try:
            return normalize_url(urljoin(base_url, href.strip())).normalized
        except UrlSafetyError:
            return None
    return None


def _safe_url(value: str | None, base_url: str) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or candidate.lower().startswith(_NON_HTTP_PREFIXES):
        return None
    try:
        return normalize_url(urljoin(base_url, candidate)).normalized
    except UrlSafetyError:
        return None


def _visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    return clean_text(root.get_text(" ", strip=True))


def _tags(soup: BeautifulSoup, text: str, metadata: dict[str, str]) -> tuple[str, ...]:
    tags: list[str] = []
    keywords = metadata.get("keywords")
    if keywords:
        tags.extend(part.strip() for part in keywords.split(",") if part.strip())
    for tag in soup.find_all("a"):
        rel = tag.get("rel")
        relations = (
            {rel.lower()} if isinstance(rel, str) else {str(value).lower() for value in rel or []}
        )
        if "tag" in relations:
            value = clean_text(tag.get_text(" ", strip=True), limit=100)
            if value:
                tags.append(value)
    tags.extend(_HASHTAG_PATTERN.findall(text))
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        normalized = clean_text(tag, limit=100)
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return tuple(result[:50])


def _links(soup: BeautifulSoup, base_url: str) -> tuple[ExtractedLink, ...]:
    results: list[ExtractedLink] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a"):
        href = anchor.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        candidate = href.strip()
        if candidate.lower().startswith(_NON_HTTP_PREFIXES):
            continue
        url = _safe_url(candidate, base_url)
        if url is None or url in seen:
            continue
        seen.add(url)
        text = clean_text(anchor.get_text(" ", strip=True), limit=300)
        original = urljoin(base_url, candidate)
        results.append(ExtractedLink(url=url, text=text, original_url=original))
        if len(results) >= _MAX_LINKS:
            break
    return tuple(results)


def _srcset_candidates(value: str) -> list[tuple[str, int | None, float | None]]:
    candidates: list[tuple[str, int | None, float | None]] = []
    for part in value.split(","):
        pieces = part.strip().split()
        if not pieces:
            continue
        url = pieces[0]
        width: int | None = None
        density: float | None = None
        if len(pieces) > 1:
            descriptor = pieces[1].strip().lower()
            try:
                if descriptor.endswith("w"):
                    width = max(0, int(float(descriptor[:-1])))
                elif descriptor.endswith("x"):
                    density = max(0.0, float(descriptor[:-1]))
            except ValueError:
                width = None
                density = None
        candidates.append((url, width, density))
    return candidates


def _is_media_path(url: str) -> bool:
    path = PurePosixPath(normalize_url(url).path)
    return normalize_extension(path.suffix) is not None


def is_direct_media_url(url: str) -> bool:
    try:
        return _is_media_path(url)
    except UrlSafetyError:
        return False


def _media(
    soup: BeautifulSoup, base_url: str, metadata: dict[str, str]
) -> tuple[MediaCandidate, ...]:
    direct_anchor_urls: set[str] = set()
    for anchor in soup.find_all("a"):
        href = anchor.get("href")
        url = _safe_url(href if isinstance(href, str) else None, base_url)
        if url is not None and is_direct_media_url(url):
            direct_anchor_urls.add(url)

    candidates: list[MediaCandidate] = []
    seen: set[str] = set()

    def add(candidate: MediaCandidate) -> None:
        if candidate.url in seen:
            return
        seen.add(candidate.url)
        candidates.append(candidate)

    for url in sorted(direct_anchor_urls):
        add(MediaCandidate(url=url, source="direct_anchor", original=True, is_preview=False))

    for image in soup.find_all("img"):
        alt_value = image.get("alt")
        alt = clean_text(alt_value, limit=300) if isinstance(alt_value, str) else None
        best_url: str | None = None
        best_width: int | None = None
        best_density: float | None = None
        srcset_selected = False
        best_score = -1.0
        srcset = image.get("srcset")
        if isinstance(srcset, str):
            for raw_url, width, density in _srcset_candidates(srcset):
                candidate_url = _safe_url(raw_url, base_url)
                if candidate_url is None or not is_direct_media_url(candidate_url):
                    continue
                score = float(width) if width is not None else (density or 0.0) * 1_000
                if score >= best_score:
                    best_url = candidate_url
                    best_score = score
                    best_width = width
                    best_density = density
                    srcset_selected = True
        if best_url is None:
            for attribute in ("data-original", "data-full", "data-src", "src"):
                value = image.get(attribute)
                candidate_url = _safe_url(value if isinstance(value, str) else None, base_url)
                if candidate_url is not None:
                    best_url = candidate_url
                    break
        if best_url is None or not is_direct_media_url(best_url):
            continue

        lowered_path = best_url.lower()
        has_full_resolution_attribute = any(
            isinstance(image.get(attribute), str) and image.get(attribute, "").strip()
            for attribute in ("data-original", "data-full")
        )
        linked_original = best_url in direct_anchor_urls
        high_resolution_srcset = srcset_selected and (
            (best_width is not None and best_width >= 600)
            or (best_width is None and best_density is not None and best_density >= 2.0)
        )
        suggests_small = any(hint in lowered_path for hint in _UNSUITABLE_IMAGE_HINTS)
        original = (
            bool(has_full_resolution_attribute or linked_original or high_resolution_srcset)
            and not suggests_small
        )
        add(
            MediaCandidate(
                url=best_url,
                source="image" if not high_resolution_srcset else "srcset",
                original=original,
                is_preview=not original,
                width=best_width,
                alt=alt,
            )
        )
        if len(candidates) >= _MAX_MEDIA:
            break

    og_image = _safe_url(metadata.get("og:image"), base_url)
    if og_image and is_direct_media_url(og_image):
        add(
            MediaCandidate(
                url=og_image,
                source="og_image",
                original=False,
                is_preview=True,
                alt="social preview",
            )
        )
    twitter_image = _safe_url(metadata.get("twitter:image"), base_url)
    if twitter_image and is_direct_media_url(twitter_image):
        add(
            MediaCandidate(
                url=twitter_image,
                source="twitter_image",
                original=False,
                is_preview=True,
                alt="social preview",
            )
        )

    return tuple(candidates[:_MAX_MEDIA])


def extract_page(html: str, final_url: str) -> ExtractedPage:
    """Parse an eligible HTML document with BeautifulSoup and lxml."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:  # pragma: no cover - exercised with a parser stub
        raise ValueError(f"HTML parsing failed ({type(exc).__name__})") from exc

    metadata = _meta_values(soup)
    text = _visible_text(soup)
    return ExtractedPage(
        final_url=normalize_url(final_url).normalized,
        title=_title(soup),
        description=metadata.get("description"),
        canonical_url=_canonical_url(soup, final_url),
        og_title=metadata.get("og:title"),
        og_description=metadata.get("og:description"),
        twitter_title=metadata.get("twitter:title"),
        twitter_description=metadata.get("twitter:description"),
        author=(
            metadata.get("author") or metadata.get("article:author") or metadata.get("book:author")
        ),
        tags=_tags(soup, text, metadata),
        text=text,
        links=_links(soup, final_url),
        media=_media(soup, final_url, metadata),
    )
