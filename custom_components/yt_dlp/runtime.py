"""Compatibility facade for runtime lookup helpers.

Protected core modules import their dedicated runtime modules directly. This
facade exists only for compatibility with code written against 0.5.12.
"""

from .download_runtime import get_loaded_manager
from .favorites_runtime import (
    get_favorites_playback_controller,
    get_favorites_store,
)
from .play_runtime import get_playback_manager

__all__ = [
    "get_loaded_manager",
    "get_playback_manager",
    "get_favorites_store",
    "get_favorites_playback_controller",
]
