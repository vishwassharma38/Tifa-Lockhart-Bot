"""Discord delivery of an actual archived media file with attribution."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import discord

from app.discovery.domains import registrable_domain
from app.reddit.models import RedditCandidate

log = logging.getLogger(__name__)

# Discord rejects content above 2,000 characters. Reserve headroom so escaping
# and client-side composition cannot push a valid archive item past the limit.
_MAX_MESSAGE_LENGTH = 2000
_SAFE_MESSAGE_BUDGET = 1800

_MAX_TITLE_LENGTH = 200
_MAX_CAPTION_LENGTH = 240
_MAX_CREATOR_LENGTH = 80
_MAX_CATEGORY_LENGTH = 80
_MAX_MARKER_LENGTH = 160
_MAX_SOURCE_LINKS = 3
_MAX_CREATOR_LINES = 2
_MAX_LINKS_PER_DOMAIN = 2

_MARKDOWN_SPECIAL = frozenset("\\`*_{}[]()#+-.!|>~")
_MARKER_SAFE_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:._-"
)
_LISTING_SEGMENTS = frozenset(
    {
        "search",
        "search.php",
        "gallery",
        "galleries",
        "tag",
        "tags",
        "filter",
        "filters",
        "browse",
        "index",
        "list",
        "latest",
        "popular",
    }
)
_PRESENTATION_QUERY_KEYS = frozenset(
    {
        "d",
        "display",
        "filter",
        "filters",
        "format",
        "lang",
        "language",
        "limit",
        "offset",
        "order",
        "p",
        "page",
        "q",
        "query",
        "s",
        "search",
        "size",
        "sort",
        "start",
        "tag",
        "tags",
        "view",
    }
)


class DiscordPublicationError(RuntimeError):
    """Raised when an attachment cannot be delivered to Discord."""


class DiscordPublicationMessageError(DiscordPublicationError):
    """Raised when application-side message construction cannot be bounded.

    This is deliberately distinct from :class:`DiscordPublicationRejectedError`
    so operators never mistake a local validation failure for a Discord
    rejection.
    """


class DiscordPublicationRejectedError(DiscordPublicationError):
    """Raised when Discord definitively rejects an attachment request."""


class DiscordPublicationUncertainError(DiscordPublicationError):
    """Raised when Discord delivery may or may not have succeeded."""


@dataclass(frozen=True, slots=True)
class MarkerLookup:
    """Result of inspecting a channel for a deterministic publication marker."""

    message_id: int | None
    conclusive: bool


@dataclass(frozen=True, slots=True)
class _RankedSource:
    row: dict[str, Any]
    url: str
    identity: str
    domain: str
    score: int
    index: int


def _clean_label(value: Any, limit: int) -> str:
    """Collapse whitespace, bound length, and escape Discord markdown."""
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return "".join(
        f"\\{character}" if character in _MARKDOWN_SPECIAL else character for character in text
    )


def _clean_marker(value: Any, limit: int) -> str:
    """Return a marker that channel-history reconciliation can match exactly.

    Markdown escaping is deliberately not applied here: the marker is an
    opaque machine token, and adding backslashes would prevent the sent message
    from containing the marker used for delivery reconciliation.
    """
    text = "".join(str(value or "").split())
    safe = "".join(character for character in text if character in _MARKER_SAFE_CHARACTERS)
    return safe[:limit]


def _safe_url(value: Any) -> str | None:
    """Return a safely escaped HTTP(S) URL, or ``None`` for unusable input."""
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(ord(char) < 32 or char.isspace() for char in text)
    ):
        return None
    try:
        _port = parsed.port
    except ValueError:
        return None
    return quote(text, safe=":/?@!$&'*,;=%+-._~")


def _url_parts(value: str) -> tuple[str, str, str] | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    path = unquote(parsed.path or "/")
    return parsed.hostname.lower().rstrip("."), path, parsed.query


def _query_keys(query: str) -> set[str]:
    try:
        return {key.lower() for key, _value in parse_qsl(query, keep_blank_values=True)}
    except ValueError:
        return set()


def _is_listing_url(path: str, query: str) -> bool:
    # A direct item page can live below a route named "gallery" or "browse"
    # (for example /gallery/tifa-artwork-12345). Its specific terminal segment
    # is stronger evidence than a generic parent segment.
    if _is_specific_source(path):
        return False
    lowered = path.lower()
    segments = {segment for segment in lowered.split("/") if segment}
    stems = {segment.rsplit(".", 1)[0] for segment in segments}
    if segments & _LISTING_SEGMENTS or stems & _LISTING_SEGMENTS:
        return True
    if re.search(r"/(?:page|p)/\d+", lowered, re.IGNORECASE):
        return True
    if _query_keys(query) & _PRESENTATION_QUERY_KEYS:
        return True
    # Gallery/search routes frequently encode a query phrase with plus signs or
    # percent-encoded spaces instead of using a query string.
    return "%20" in path or "+" in path or " " in path


def _is_specific_source(path: str) -> bool:
    last_segment = path.rsplit("/", 1)[-1].lower()
    # Generic listing endpoints often end in .php/.html; their stem is more
    # informative than the extension and must not make them look specific.
    if last_segment in _LISTING_SEGMENTS or last_segment.rsplit(".", 1)[0] in _LISTING_SEGMENTS:
        return False
    if re.search(r"\d{3,}", last_segment):
        return True
    if re.search(r"[0-9a-f]{12,}", last_segment, re.IGNORECASE):
        return True
    return last_segment.endswith((".html", ".htm", ".php"))


def _source_identity(host: str, path: str, query: str) -> str:
    normalized_path = path.rstrip("/") or "/"
    keys = _query_keys(query)
    if not keys or keys <= _PRESENTATION_QUERY_KEYS:
        return f"{host}{normalized_path}"
    pairs = sorted(parse_qsl(query, keep_blank_values=True))
    rendered = "&".join(f"{key}={value}" for key, value in pairs)
    return f"{host}{normalized_path}?{rendered}"


def _rank_sources(
    sources: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[_RankedSource]:
    """Rank provenance deterministically, preferring specific media pages.

    Generic gallery, search, pagination, and presentation-filter URLs are
    retained in ``media_sources`` but are deliberately ranked below a direct
    image-specific page. Identical presentation variants collapse to one
    candidate while the database provenance remains untouched.
    """
    ranked: list[_RankedSource] = []
    seen_identities: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            continue
        url = _safe_url(source.get("source_url"))
        if url is None:
            continue
        parts = _url_parts(url)
        if parts is None:
            continue
        host, path, query = parts
        identity = _source_identity(host, path, query)
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        listing = _is_listing_url(path, query)
        specific = not listing and _is_specific_source(path)
        score = 0
        if listing:
            score -= 60
        elif specific:
            score += 60
        if str(source.get("creator_name") or "").strip():
            score += 25
        if _safe_url(source.get("creator_profile_url")):
            score += 15
        if str(source.get("title") or "").strip():
            score += 10
        if str(source.get("caption") or "").strip():
            score += 5
        if len(url) > 300:
            score -= 5
        try:
            domain = registrable_domain(host)
        except Exception:
            domain = host
        ranked.append(
            _RankedSource(
                row=source,
                url=url,
                identity=identity,
                domain=domain,
                score=score,
                index=index,
            )
        )
    ranked.sort(key=lambda item: (-item.score, item.domain, item.identity, item.index))
    return ranked


def _select_source_links(ranked: list[_RankedSource]) -> list[_RankedSource]:
    """Choose a small, domain-diverse set of useful source links."""
    if not ranked:
        return []
    selected = [ranked[0]]
    domain_counts: dict[str, int] = {ranked[0].domain: 1}
    seen = {ranked[0].identity}

    def remaining() -> list[_RankedSource]:
        return [
            candidate
            for candidate in ranked[1:]
            if candidate.identity not in seen
            and domain_counts.get(candidate.domain, 0) < _MAX_LINKS_PER_DOMAIN
        ]

    # Prefer a different domain before adding a second link from the same one.
    for candidate in remaining():
        if len(selected) >= _MAX_SOURCE_LINKS:
            break
        if candidate.domain in domain_counts:
            continue
        selected.append(candidate)
        seen.add(candidate.identity)
        domain_counts[candidate.domain] = domain_counts.get(candidate.domain, 0) + 1

    # If every remaining source shares the primary domain, allow one bounded
    # additional reference without turning the message into a link dump.
    for candidate in remaining():
        if len(selected) >= _MAX_SOURCE_LINKS:
            break
        selected.append(candidate)
        seen.add(candidate.identity)
        domain_counts[candidate.domain] = domain_counts.get(candidate.domain, 0) + 1
    return selected


@dataclass(frozen=True, slots=True)
class PublicationContext:
    """Provider-neutral attribution for a published media file.

    Reddit candidates remain supported through :meth:`from_candidate`, while
    archived web discoveries and digest records use only the provenance that
    actually exists in the database.
    """

    title: str | None = None
    caption: str | None = None
    creator_name: str | None = None
    creator_profile_url: str | None = None
    source_url: str | None = None
    source_platform: str | None = None
    source_post_id: str | None = None
    category: str | None = None
    publication_marker: str | None = None
    sources: tuple[dict[str, Any], ...] = ()
    media_type: str | None = None

    @classmethod
    def from_candidate(cls, candidate: RedditCandidate) -> PublicationContext:
        return cls(
            title=candidate.title,
            caption=candidate.body,
            creator_name=candidate.author_name,
            creator_profile_url=candidate.author_profile_url,
            source_url=candidate.source_url,
            source_platform=getattr(candidate, "source_platform", "reddit") or "reddit",
            source_post_id=getattr(candidate, "source_post_id", None) or candidate.submission_id,
        )

    @classmethod
    def from_media_record(
        cls,
        media: dict[str, Any],
        source: dict[str, Any] | list[dict[str, Any]] | None = None,
        *,
        publication_marker: str | None = None,
    ) -> PublicationContext:
        sources = tuple(source) if isinstance(source, list) else ((source,) if source else ())
        ranked = _rank_sources(sources)
        primary = ranked[0] if ranked else None
        primary_row = primary.row if primary else {}
        return cls(
            title=media.get("title") or primary_row.get("title"),
            caption=media.get("caption") or primary_row.get("caption"),
            creator_name=primary_row.get("creator_name"),
            creator_profile_url=primary_row.get("creator_profile_url"),
            source_url=primary.url if primary else primary_row.get("source_url"),
            source_platform=primary_row.get("source_platform"),
            source_post_id=primary_row.get("source_post_id"),
            category=media.get("category"),
            publication_marker=publication_marker,
            sources=sources,
            media_type=media.get("media_type"),
        )


def _as_context(value: PublicationContext | RedditCandidate) -> PublicationContext:
    if isinstance(value, PublicationContext):
        return value
    return PublicationContext.from_candidate(value)


def build_publication_message(
    value: PublicationContext | RedditCandidate,
) -> str:
    """Build truthful, bounded caption text without inventing attribution."""
    context = _as_context(value)
    associations = list(context.sources)
    if context.source_url and not any(
        str(source.get("source_url") or "").strip() == str(context.source_url).strip()
        for source in associations
        if isinstance(source, dict)
    ):
        associations.append({"source_url": context.source_url})
    ranked = _rank_sources(associations)
    primary = ranked[0] if ranked else None
    selected = _select_source_links(ranked)
    primary_row = primary.row if primary else {}

    title = context.title or primary_row.get("title")
    caption = context.caption or primary_row.get("caption")
    creator_name = context.creator_name or primary_row.get("creator_name")
    creator_profile = context.creator_profile_url or primary_row.get("creator_profile_url")
    category = context.category
    platform = context.source_platform or primary_row.get("source_platform")
    source_post_id = context.source_post_id or primary_row.get("source_post_id")

    media_type = _clean_label(context.media_type, 40).upper() or "MEDIA"
    heading = f"**TIFA ARCHIVE · {media_type}**"
    marker_text = _clean_marker(context.publication_marker, _MAX_MARKER_LENGTH)
    marker_line = f"Publication: {marker_text}" if marker_text else ""

    parts: list[str] = [heading]

    def reserved_length() -> int:
        return len(marker_line) + 1 if marker_line else 0

    def fits(candidate: str) -> bool:
        return len("\n".join([*parts, candidate])) + reserved_length() <= _SAFE_MESSAGE_BUDGET

    def add_bounded(prefix: str, value: Any, limit: int, minimum: int = 12) -> bool:
        raw = " ".join(str(value or "").split())
        if not raw:
            return False
        text = raw[:limit]
        while text:
            candidate = prefix + _clean_label(text, limit)
            if fits(candidate):
                parts.append(candidate)
                return True
            if len(text) <= minimum:
                return False
            text = text[: max(minimum, len(text) - 24)].rstrip()
        return False

    def add_optional(candidate: str) -> bool:
        if fits(candidate):
            parts.append(candidate)
            return True
        return False

    add_bounded("", title, _MAX_TITLE_LENGTH)
    add_bounded("", caption, _MAX_CAPTION_LENGTH, minimum=24)

    creators: list[tuple[str, str | None]] = []
    seen_creators: set[str] = set()

    def add_creator(name: Any, profile: Any) -> None:
        cleaned = " ".join(str(name or "").split())
        if not cleaned or len(creators) >= _MAX_CREATOR_LINES:
            return
        key = cleaned.casefold()
        if key in seen_creators:
            return
        seen_creators.add(key)
        creators.append((cleaned, str(profile or "").strip() or None))

    add_creator(creator_name, creator_profile)
    # Creator attribution is independent of URL de-duplication: several source
    # rows can share one URL while naming different artists. Keep at most two
    # bounded creator lines from the original provenance set.
    for source in associations:
        if isinstance(source, dict):
            add_creator(source.get("creator_name"), source.get("creator_profile_url"))
    creator_profile_urls: set[str] = set()
    if creators:
        for name, profile in creators:
            safe_profile = _safe_url(profile) if profile else None
            if safe_profile:
                creator_profile_urls.add(safe_profile)
                candidate = f"Creator: [{_clean_label(name, _MAX_CREATOR_LENGTH)}]({safe_profile})"
                if fits(candidate):
                    parts.append(candidate)
                else:
                    add_optional(
                        f"Creator: {_clean_label(name, _MAX_CREATOR_LENGTH)} (profile link omitted)"
                    )
            else:
                add_optional(
                    f"Creator: {_clean_label(name, _MAX_CREATOR_LENGTH)} "
                    "(profile not provided by source)"
                )
    else:
        add_optional("Creator: not provided by source")

    add_bounded("Category: ", category, _MAX_CATEGORY_LENGTH)

    visible_sources = list(selected)
    while visible_sources:
        links: list[str] = []
        for index, candidate in enumerate(visible_sources):
            # Never drop the primary source link merely because the source row
            # also supplied the same URL as its creator profile.
            if index > 0 and candidate.url in creator_profile_urls:
                continue
            label = "Original source" if index == 0 else "Additional source"
            links.append(f"[{label}]({candidate.url})")
        omitted = max(0, len(associations) - len(visible_sources))
        if links:
            suffix = ""
            if omitted:
                suffix = f" (+{omitted} more archived reference{'s' if omitted != 1 else ''})"
            line = "Sources: " + " · ".join(links) + suffix
            if fits(line):
                parts.append(line)
                visible_sources = []
                break
            line_without_suffix = "Sources: " + " · ".join(links)
            if suffix and fits(line_without_suffix):
                parts.append(line_without_suffix)
                visible_sources = []
                break
        visible_sources.pop()
    if not any(part.startswith("Sources: ") for part in parts):
        if primary is not None:
            parts_info = _url_parts(primary.url)
            host = parts_info[0] if parts_info else "stored metadata"
            add_optional(f"Source: {host} (link too long for Discord; archived in metadata)")
        else:
            add_optional("Source: not available from stored metadata")

    if marker_line:
        if not add_optional(marker_line):
            # The marker is required for safe reconciliation. It is bounded by
            # construction, so this only occurs with an invalid caller value.
            raise DiscordPublicationMessageError("Publication marker exceeds message budget")

    add_bounded("Source platform: ", platform, 60)
    if source_post_id and str(platform or "").strip().lower() != "web":
        add_bounded("Source ID: ", source_post_id, 80)

    message = "\n".join(parts)
    if len(message) > _SAFE_MESSAGE_BUDGET:
        message = _compact_publication_message(
            heading=heading,
            title=title,
            primary=primary,
            marker_line=marker_line,
        )
    if len(message) > _MAX_MESSAGE_LENGTH:
        raise DiscordPublicationMessageError("Publication message could not be bounded safely")
    return message


def _compact_publication_message(
    *,
    heading: str,
    title: Any,
    primary: _RankedSource | None,
    marker_line: str,
) -> str:
    """Return a minimal valid message if optional attribution consumed the budget."""
    parts = [heading]
    title_text = " ".join(str(title or "").split())
    if title_text:
        candidate = _clean_label(title_text, 120)
        if len("\n".join([*parts, candidate, marker_line])) <= _SAFE_MESSAGE_BUDGET:
            parts.append(candidate)
    if primary is not None:
        if len("\n".join([*parts, f"[Original source]({primary.url})", marker_line])) <= (
            _SAFE_MESSAGE_BUDGET
        ):
            parts.append(f"[Original source]({primary.url})")
        else:
            parts_info = _url_parts(primary.url)
            host = parts_info[0] if parts_info else "stored metadata"
            parts.append(f"Source: {host} (archived in metadata)")
    if marker_line:
        parts.append(marker_line)
    message = "\n".join(parts)
    if len(message) > _MAX_MESSAGE_LENGTH:
        raise DiscordPublicationMessageError("Publication message could not be bounded safely")
    return message


def publication_error_from_discord_exception(
    exc: BaseException,
) -> DiscordPublicationError:
    """Translate a Discord send failure into a precise, secret-free category."""
    if isinstance(exc, discord.HTTPException):
        status = int(getattr(exc, "status", 0) or 0)
        if 400 <= status < 500 and status != 429:
            return DiscordPublicationRejectedError(
                f"Discord rejected attachment delivery (HTTP {status})"
            )
        return DiscordPublicationUncertainError(
            f"Discord attachment delivery outcome is uncertain (HTTP {status or 'unknown'})"
        )
    return DiscordPublicationUncertainError(
        f"Discord attachment delivery outcome is uncertain ({type(exc).__name__})"
    )


class DiscordPublisher:
    """Send a bounded media payload as a real Discord file attachment."""

    def __init__(self, bot: Any, channel_id: int | None = None) -> None:
        self._bot = bot
        self._channel_id = channel_id or 0

    @property
    def channel_id(self) -> int:
        return self._channel_id

    async def _resolve_channel(self, channel_id: int) -> Any:
        if channel_id <= 0:
            raise DiscordPublicationError("Configured Discord channel is unavailable")
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            fetch_channel = getattr(self._bot, "fetch_channel", None)
            if callable(fetch_channel):
                try:
                    channel = await fetch_channel(channel_id)
                except Exception as exc:
                    raise DiscordPublicationError(
                        f"Configured Discord channel is unavailable ({type(exc).__name__})"
                    ) from None
        if channel is None:
            raise DiscordPublicationError("Configured Discord channel is unavailable")
        return channel

    async def publish(
        self,
        *,
        filename: str,
        data: bytes,
        candidate: RedditCandidate | None = None,
        context: PublicationContext | None = None,
        channel_id: int | None = None,
    ) -> int:
        if candidate is None and context is None:
            raise DiscordPublicationError("Publication attribution is required")
        target_channel = channel_id or self._channel_id
        # Build and validate before allocating upload resources so a failed
        # construction cannot leak an open file stream.
        content = build_publication_message(context or candidate)
        channel = await self._resolve_channel(target_channel)

        buffer = io.BytesIO(data)
        file: discord.File | None = None
        try:
            file = discord.File(buffer, filename=filename)
            try:
                message = await channel.send(
                    content=content,
                    file=file,
                    suppress_embeds=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise publication_error_from_discord_exception(exc) from None
        finally:
            # discord.File does not close caller-owned buffers, so release the
            # BytesIO explicitly after detaching it from the upload object. Guard
            # construction so a failed upload allocation cannot leak it.
            if file is not None:
                file.close()
            buffer.close()

        message_id = getattr(message, "id", None)
        if message_id is None:
            raise DiscordPublicationUncertainError("Discord message ID was not returned")
        return int(message_id)

    async def find_publication_marker(
        self,
        *,
        channel_id: int,
        marker: str,
        limit: int = 500,
    ) -> MarkerLookup:
        """Inspect channel history for a stable digest publication marker.

        A missing marker is only conclusive when the bounded history scan did
        not hit its limit. Callers must fail closed on an inconclusive scan.
        """
        channel = await self._resolve_channel(channel_id)
        history = getattr(channel, "history", None)
        if not callable(history):
            raise DiscordPublicationError("Discord message history is unavailable")

        seen = 0
        try:
            async for message in history(limit=max(1, limit)):
                seen += 1
                content = getattr(message, "content", "") or ""
                if marker in content:
                    message_id = getattr(message, "id", None)
                    if message_id is None:
                        raise DiscordPublicationError(
                            "Discord message history returned an invalid message"
                        )
                    return MarkerLookup(message_id=int(message_id), conclusive=True)
                for embed in getattr(message, "embeds", None) or ():
                    footer = getattr(embed, "footer", None)
                    footer_text = getattr(footer, "text", None)
                    if isinstance(footer_text, str) and marker in footer_text:
                        message_id = getattr(message, "id", None)
                        if message_id is None:
                            raise DiscordPublicationError(
                                "Discord message history returned an invalid message"
                            )
                        return MarkerLookup(message_id=int(message_id), conclusive=True)
        except DiscordPublicationError:
            raise
        except Exception as exc:
            raise DiscordPublicationError(
                f"Discord message history lookup failed ({type(exc).__name__})"
            ) from None

        return MarkerLookup(message_id=None, conclusive=seen < max(1, limit))
