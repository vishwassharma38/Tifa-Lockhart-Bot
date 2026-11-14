"""Bounded HTML metadata, link, and media-candidate extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.discovery.links import score_link
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
    kind: str = "anchor"
    context: str = ""
    quality_score: int = 0
    quality_reasons: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "text": self.text[:300],
            "kind": self.kind,
            "quality_score": self.quality_score,
            "quality_reasons": list(self.quality_reasons),
        }


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


def _document_base(soup: BeautifulSoup, final_url: str) -> str:
    """Resolve relative references against the first valid HTML base element."""
    tag = soup.find("base", href=True)
    href = tag.get("href") if tag is not None else None
    if not isinstance(href, str) or not href.strip():
        return final_url
    try:
        return normalize_url(urljoin(final_url, href.strip())).normalized
    except UrlSafetyError:
        return final_url


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
    best_by_url: dict[str, ExtractedLink] = {}
    candidates: list[tuple[Any, str]] = [(tag, "anchor") for tag in soup.find_all("a")]
    candidates.extend((tag, "area") for tag in soup.find_all("area"))
    useful_relations = {"alternate", "author", "next", "prev", "related", "tag"}
    for tag in soup.find_all("link"):
        raw_rel = tag.get("rel") or []
        relations = {str(value).lower() for value in raw_rel}
        chosen = sorted(relations & useful_relations)
        if chosen:
            candidates.append((tag, chosen[0]))

    for tag, kind in candidates:
        href = tag.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        candidate = href.strip()
        if candidate.lower().startswith(_NON_HTTP_PREFIXES):
            continue
        url = _safe_url(candidate, base_url)
        if url is None:
            continue
        text_value = tag.get("aria-label") or tag.get("title")
        text = clean_text(
            tag.get_text(" ", strip=True) or (text_value if isinstance(text_value, str) else ""),
            limit=300,
        )
        parent = tag.parent
        parent_name = getattr(parent, "name", "")
        context = ""
        if parent is not None and parent_name not in {
            "body",
            "main",
            "nav",
            "header",
            "footer",
            "aside",
            "ul",
            "ol",
        }:
            context = clean_text(parent.get_text(" ", strip=True), limit=500)
        quality, reasons, trap = score_link(url, text=text, context=context, kind=kind)
        if trap:
            continue
        original = urljoin(base_url, candidate)
        link = ExtractedLink(
            url=url,
            text=text,
            original_url=original,
            kind=kind,
            context=context,
            quality_score=quality,
            quality_reasons=reasons,
        )
        current = best_by_url.get(url)
        if current is None or (link.quality_score, bool(link.text)) > (
            current.quality_score,
            bool(current.text),
        ):
            best_by_url[url] = link
        if len(best_by_url) >= _MAX_LINKS:
            break
    return tuple(best_by_url.values())


def extract_http_link_header(value: str | None, base_url: str) -> tuple[ExtractedLink, ...]:
    """Extract useful RFC 8288 Link relations without following canonical blindly."""
    if not value:
        return ()

    members: list[str] = []
    start = 0
    angle_depth = 0
    quoted = False
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quoted:
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif not quoted and character == "<":
            angle_depth += 1
        elif not quoted and character == ">":
            angle_depth = max(0, angle_depth - 1)
        elif not quoted and angle_depth == 0 and character == ",":
            members.append(value[start:index].strip())
            start = index + 1
    members.append(value[start:].strip())

    results: list[ExtractedLink] = []
    seen: set[str] = set()
    useful = {"author", "next", "prev", "related", "tag", "alternate"}
    for member in members:
        if not member.startswith("<") or ">" not in member:
            continue
        end = member.find(">")
        reference = member[1:end].strip()
        parameters = member[end + 1 :]
        relation_match = re.search(
            r"(?:^|;)\s*rel\s*=\s*(?:\"([^\"]*)\"|([^;\s]+))", parameters, re.I
        )
        if relation_match is None:
            continue
        relations = (relation_match.group(1) or relation_match.group(2) or "").lower().split()
        relation = next((item for item in relations if item in useful), None)
        if relation is None:
            continue
        title_match = re.search(
            r"(?:^|;)\s*title\s*=\s*(?:\"([^\"]*)\"|([^;\s]+))", parameters, re.I
        )
        title = (
            clean_text(title_match.group(1) or title_match.group(2), limit=300)
            if title_match
            else ""
        )
        url = _safe_url(reference, base_url)
        if url is None or url in seen:
            continue
        quality, reasons, trap = score_link(url, text=title, kind=relation)
        if trap:
            continue
        seen.add(url)
        results.append(
            ExtractedLink(
                url=url,
                text=title,
                original_url=urljoin(base_url, reference),
                kind=relation,
                context="HTTP Link header",
                quality_score=quality,
                quality_reasons=reasons,
            )
        )
        if len(results) >= 100:
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

    effective_url = normalize_url(final_url).normalized
    base_url = _document_base(soup, effective_url)
    metadata = _meta_values(soup)
    text = _visible_text(soup)
    return ExtractedPage(
        final_url=effective_url,
        title=_title(soup),
        description=metadata.get("description"),
        canonical_url=_canonical_url(soup, base_url),
        og_title=metadata.get("og:title"),
        og_description=metadata.get("og:description"),
        twitter_title=metadata.get("twitter:title"),
        twitter_description=metadata.get("twitter:description"),
        author=(
            metadata.get("author") or metadata.get("article:author") or metadata.get("book:author")
        ),
        tags=_tags(soup, text, metadata),
        text=text,
        links=_links(soup, base_url),
        media=_media(soup, base_url, metadata),
    )
