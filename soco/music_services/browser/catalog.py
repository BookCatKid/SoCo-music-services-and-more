'''Manifest and presentation-map data for configured music services.

Every music service advertises a JSON ``manifest`` (when it has one) through
its descriptor, and most services describe their search, display and artwork
rules in an XML presentation map.  This module parses both into usable
structures.  The browser keeps the parsed results cached in memory, so the
documents are only fetched once per :class:`MusicServiceBrowser` instance.
'''

from __future__ import unicode_literals

import requests

from ...exceptions import MusicServiceException
from ...xml import XML
from .util import _as_mapping, _as_string


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]


class PresentationMap:
    """A parsed music-service presentation map.

    The presentation map is the XML document which tells controllers how to
    present a service: which search categories and variants exist, which
    display types to use for each container, how artwork and browse icons
    scale, and which quality badges streams may carry.  Every attribute is
    parsed into plain dicts/lists so the document is easy to inspect and
    serialize; the original XML is retained in :attr:`raw_xml`.
    """

    def __init__(
        self,
        uri="",
        version=None,
        page_size=None,
        artwork_size_map=None,
        browse_icon_size_map=None,
        display_types=None,
        search_categories=None,
        menu_item_overrides=None,
        stream_quality_badges=None,
        now_playing_ratings=None,
        quick_skips=None,
        raw_xml="",
    ):
        self.uri = uri
        self.version = version
        self.page_size = page_size
        self.artwork_size_map = dict(artwork_size_map or {})
        self.browse_icon_size_map = dict(browse_icon_size_map or {})
        self.display_types = dict(display_types or {})
        self.search_categories = {
            variant: list(entries)
            for variant, entries in (search_categories or {}).items()
        }
        self.menu_item_overrides = list(menu_item_overrides or [])
        self.stream_quality_badges = dict(stream_quality_badges or {})
        self.now_playing_ratings = list(now_playing_ratings or [])
        self.quick_skips = {
            skip_type: dict(entry)
            for skip_type, entry in (quick_skips or {}).items()
        }
        self.raw_xml = raw_xml

    def __repr__(self):
        return "<{} uri={!r} version={} at {}>".format(
            self.__class__.__name__, self.uri, self.version, hex(id(self))
        )

    def search_variants(self):
        """dict: The legacy ``{category: [(variant, mapped_id)]}`` mapping.

        This is the same structure used by
        :meth:`MusicService._get_search_variants`, so parsed presentation-map
        data can feed search directly.  Categories which appear in several
        ``<SearchCategories>`` blocks keep every ``(variant, mapped_id)``
        pair, in document order.
        """
        variants = {}
        for variant, entries in self.search_categories.items():
            for entry in entries:
                variants.setdefault(entry["id"], []).append(
                    (variant, entry["mapped_id"])
                )
        return variants


def parse_presentation_map(payload, uri="", version=None):
    """Parse presentation-map XML bytes into a :class:`PresentationMap`.

    Args:
        payload (bytes): The raw presentation-map document.
        uri (str): Where the document was fetched from, kept for reference.
        version: The version advertised by the manifest, if any.

    Returns:
        :class:`PresentationMap`: The parsed model.

    Raises:
        XML.ParseError: If ``payload`` is not well-formed XML.
    """
    root = XML.fromstring(payload)

    page_size = None
    browse_options = next(
        (element for element in root if _local_name(element.tag) == "BrowseOptions"),
        None,
    )
    if browse_options is not None and browse_options.get("PageSize"):
        try:
            page_size = int(browse_options.get("PageSize"))
        except ValueError:
            pass

    artwork_size_map = {}
    browse_icon_size_map = {}
    display_types = {}
    search_categories = {}
    menu_item_overrides = []
    stream_quality_badges = {}
    now_playing_ratings = []
    quick_skips = {}

    for block in root:
        if _local_name(block.tag) != "PresentationMap":
            continue
        block_type = block.get("type", "")
        if block_type == "ArtWorkSizeMap":
            artwork_size_map = _parse_size_entries(block)
        elif block_type == "BrowseIconSizeMap":
            browse_icon_size_map = _parse_size_entries(block)
        elif block_type == "DisplayType":
            display_types = _parse_display_types(block)
        elif block_type == "Search":
            search_categories = _parse_search_categories(block)
        elif block_type == "InfoView":
            menu_item_overrides = _parse_menu_item_overrides(block)
        elif block_type == "StreamQualityBadgeDictionary":
            stream_quality_badges = _parse_quality_badges(block)
        elif block_type in ("NowPlayingRatings", "NowPlayingRatings_v2"):
            now_playing_ratings.extend(_parse_now_playing_ratings(block))
        elif block_type == "QuickSkips":
            quick_skips = _parse_quick_skips(block)

    return PresentationMap(
        uri=uri,
        version=version,
        page_size=page_size,
        artwork_size_map=artwork_size_map,
        browse_icon_size_map=browse_icon_size_map,
        display_types=display_types,
        search_categories=search_categories,
        menu_item_overrides=menu_item_overrides,
        stream_quality_badges=stream_quality_badges,
        now_playing_ratings=now_playing_ratings,
        quick_skips=quick_skips,
        raw_xml=payload.decode("utf-8", "replace"),
    )


def _parse_size_entries(block):
    """Parse an ``imageSizeMap``/``browseIconSizeMap`` block into {size: sub}."""
    result = {}
    for entry in block.iter():
        if _local_name(entry.tag) != "sizeEntry":
            continue
        size = entry.get("size")
        substitution = entry.get("substitution")
        if not size or not substitution:
            continue
        try:
            result[int(size)] = substitution
        except ValueError:
            continue
    return result


def _parse_display_types(block):
    """Parse a ``DisplayType`` block into {id: {display_mode, lines, ...}}."""
    result = {}
    for node in block:
        name = _local_name(node.tag)
        if name == "RootNodeDisplayType":
            result["__root__"] = _parse_display_node(node)
        elif name == "DisplayType" and node.get("id"):
            result[node.get("id")] = _parse_display_node(node)
    return result


def _parse_display_node(node):
    parsed = {}
    for child in node:
        name = _local_name(child.tag)
        if name == "DisplayMode":
            parsed["display_mode"] = (child.text or "").strip()
        elif name in ("Lines", "Header"):
            lines = [
                parsed_line
                for line in child
                if _local_name(line.tag) == "Line"
                for parsed_line in [_parse_line(line)]
                if parsed_line is not None
            ]
            if lines:
                parsed["lines" if name == "Lines" else "header"] = lines
        elif name == "ItemThumbnails":
            parsed["item_thumbnails"] = child.get("source", "")
    return parsed


def _parse_line(line):
    if line.get("token"):
        return {"token": line.get("token")}
    if line.get("stringId"):
        return {"string_id": line.get("stringId")}
    return None


def _parse_search_categories(block):
    """Parse a ``Search`` block into {variant: [{id, mapped_id, custom}]}."""
    result = {}
    for categories in block.iter():
        if _local_name(categories.tag) != "SearchCategories":
            continue
        variant = categories.get("stringId", "default")
        entries = result.setdefault(variant, [])
        for category in categories:
            name = _local_name(category.tag)
            if name == "Category":
                entries.append(
                    {
                        "id": category.get("id", ""),
                        "mapped_id": category.get("mappedId")
                        or category.get("id", ""),
                        "custom": False,
                    }
                )
            elif name == "CustomCategory":
                entries.append(
                    {
                        "id": category.get("stringId", ""),
                        "mapped_id": category.get("mappedId", ""),
                        "custom": True,
                    }
                )
    return result


def _parse_menu_item_overrides(block):
    """Parse an ``InfoView`` block into the menu-item override attributes."""
    return [
        dict(item.attrib)
        for item in block.iter()
        if _local_name(item.tag) == "MenuItem"
    ]


def _parse_quality_badges(block):
    """Parse a ``StreamQualityBadgeDictionary`` block into {id: text}."""
    result = {}
    for badge in block.iter():
        if _local_name(badge.tag) != "QualityBadgeMap":
            continue
        if badge.get("id"):
            result[badge.get("id")] = badge.get("text", "")
    return result


def _parse_now_playing_ratings(block):
    """Parse a ``NowPlayingRatings`` block into per-rating entries.

    Each ``<Match>`` groups one vote/rating state (``propname``/``value``) and
    every ``<Rating>`` under it carries its ids, skip behavior and one icon
    URL per controller.  ``NowPlayingRatings_v2`` additionally carries
    ``type``/``state`` attributes on both the match and the rating.
    """
    result = []
    for match in block.iter():
        if _local_name(match.tag) != "Match":
            continue
        for rating in match.iter():
            if _local_name(rating.tag) != "Rating":
                continue
            icons = {}
            for icon in rating:
                if _local_name(icon.tag) != "Icon":
                    continue
                if icon.get("Controller") and icon.get("Uri"):
                    icons[icon.get("Controller")] = icon.get("Uri")
            result.append(
                {
                    "propname": match.get("propname", ""),
                    "value": match.get("value", ""),
                    "type": match.get("type"),
                    "rating": {
                        "id": rating.get("Id", ""),
                        "string_id": rating.get("StringId", ""),
                        "auto_skip": rating.get("AutoSkip", ""),
                        "on_success_string_id": rating.get(
                            "OnSuccessStringId", ""
                        ),
                        "type": rating.get("Type"),
                        "state": rating.get("State"),
                        "icons": icons,
                    },
                }
            )
    return result


def _parse_quick_skips(block):
    """Parse a ``QuickSkips`` block into {type: {forward, backward seconds}}."""
    result = {}
    for skip in block.iter():
        if _local_name(skip.tag) != "QuickSkip":
            continue
        skip_type = skip.get("type", "")
        if not skip_type:
            continue
        entry = {}
        for attr, key in (("forwardSeconds", "forward_seconds"),
                          ("backwardSeconds", "backward_seconds")):
            raw = skip.get(attr)
            if raw:
                try:
                    entry[key] = int(raw)
                except ValueError:
                    pass
        result[skip_type] = entry
    return result


def _resolve_presentation_map_uri(music_service, manifest=None):
    """Return the service presentation-map URI, or ``""`` when none exists.

    The descriptor's ``PresentationMapUri`` wins when advertised; otherwise
    the JSON manifest's ``presentationMap.uri`` entry is used (Apple-style
    services deliver the presentation map only through the manifest).
    """
    if music_service.presentation_map_uri:
        return music_service.presentation_map_uri
    if manifest:
        entry = _as_mapping(manifest.get("presentationMap"))
        return _as_string(entry.get("uri"))
    return ""


def _fetch_presentation_map(music_service, session, manifest=None):
    """Fetch and parse a service's presentation map.

    The URI is resolved from the descriptor or the (already fetched)
    ``manifest``.  Returns ``None`` when the service does not advertise a
    presentation map.  Network and parse failures raise
    :class:`MusicServiceException`; callers decide when to pay that cost.

    Args:
        music_service: The legacy descriptor object.
        session: The shared requests session.
        manifest (dict, optional): The parsed service manifest, when already
            available.

    Returns:
        :class:`PresentationMap` or ``None``.
    """
    uri = _resolve_presentation_map_uri(music_service, manifest)
    if not uri:
        return None
    version = None
    if manifest:
        entry = _as_mapping(manifest.get("presentationMap"))
        version = entry.get("version")
    try:
        response = session.get(
            uri, headers={"Accept": "application/xml"}, timeout=20
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise MusicServiceException(
            "{} presentation map request failed: {}".format(
                music_service.service_name, error
            )
        ) from error
    try:
        return parse_presentation_map(response.content, uri=uri, version=version)
    except XML.ParseError as error:
        raise MusicServiceException(
            "{} presentation map was not valid XML".format(
                music_service.service_name
            )
        ) from error
