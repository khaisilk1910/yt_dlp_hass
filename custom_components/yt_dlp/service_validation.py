"""Shared service validators for YouTube-DLP.

This module is intentionally free of playback/download runtime logic.  Service
modules import only the validators they need, which keeps the download and
speaker-playback paths independent from Favorites/UI changes.
"""

from __future__ import annotations

from urllib.parse import urlparse

import voluptuous as vol

import homeassistant.helpers.config_validation as cv


def http_url(value: str) -> str:
    """Validate an HTTP(S) URL accepted by yt-dlp."""
    value = cv.string(value).strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise vol.Invalid("expected an HTTP or HTTPS URL")
    return value


def search_query(value: str) -> str:
    """Validate and normalize a search query."""
    value = cv.string(value).strip()
    if not value:
        raise vol.Invalid("search query must not be empty")
    if len(value) > 200:
        raise vol.Invalid("search query is too long")
    return value


def media_player_entity(value: str) -> str:
    """Validate a single media_player entity id."""
    value = cv.entity_id(value)
    if not value.startswith("media_player."):
        raise vol.Invalid("expected a media_player entity")
    return value


def media_player_entities(value: object) -> list[str]:
    """Validate one or more unique media_player entity ids."""
    raw_items = [value] if isinstance(value, str) else value
    if not isinstance(raw_items, (list, tuple)):
        raise vol.Invalid("expected a list of media_player entities")

    entities: list[str] = []
    for item in raw_items:
        entity_id = media_player_entity(item)
        if entity_id not in entities:
            entities.append(entity_id)

    if not entities:
        raise vol.Invalid("at least one media_player entity is required")
    if len(entities) > 32:
        raise vol.Invalid("a maximum of 32 media_player entities is supported")
    return entities


def favorite_keys(value: object) -> list[str]:
    """Validate a bounded list of persisted favorite/library identifiers."""
    raw_items = [value] if isinstance(value, str) else value
    if not isinstance(raw_items, (list, tuple)):
        raise vol.Invalid("expected a list of favorite identifiers")

    result: list[str] = []
    for item in raw_items:
        key = cv.string(item).strip()
        if key and key not in result:
            result.append(key)
        if len(result) > 1000:
            raise vol.Invalid("a maximum of 1000 favorite identifiers is supported")
    return result
