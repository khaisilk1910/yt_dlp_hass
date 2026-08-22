"""Blocking helpers for the YouTube-DLP integration."""

from __future__ import annotations

import os
import shutil
import tempfile
from types import ModuleType
from typing import Any


def normalize_download_directory(path: str) -> str:
    """Normalize a configured download path without performing file-system I/O."""
    value = path.strip()
    if not value:
        raise ValueError("Download directory must not be empty")
    return os.path.abspath(os.path.expanduser(value))


def ensure_writable_directory(path: str) -> str:
    """Create and validate a writable download directory.

    This function performs file-system I/O and must only be called from a worker
    thread (config-flow executor or a download worker), never from the HA event
    loop.
    """
    normalized = normalize_download_directory(path)
    os.makedirs(normalized, mode=0o755, exist_ok=True)
    if not os.path.isdir(normalized):
        raise OSError(f"Not a directory: {normalized}")

    try:
        with tempfile.NamedTemporaryFile(
            dir=normalized,
            prefix=".yt_dlp_write_test_",
        ):
            pass
    except OSError as err:
        raise OSError(f"Directory is not writable: {normalized}") from err

    return normalized


def detect_javascript_runtime() -> tuple[str, str] | None:
    """Detect one external JavaScript runtime supported by yt-dlp.

    Path lookups and the bundled Deno lookup may touch the file system, so this
    helper is intentionally called lazily from yt-dlp executor workers rather
    than during Home Assistant startup.
    """
    # Deno is yt-dlp's recommended runtime. Prefer a system Deno first, then the
    # official PyPI redistribution declared by this integration. This avoids
    # accidentally selecting an older system Node before the known-good bundled
    # Deno binary.
    if path := shutil.which("deno"):
        return "deno", path

    try:
        import deno  # type: ignore[import-not-found]

        bundled = deno.find_deno_bin()
    except (ImportError, AttributeError, OSError, RuntimeError):
        bundled = None
    if bundled and os.path.isfile(bundled):
        return "deno", bundled

    for runtime, executable in (
        ("node", "node"),
        ("bun", "bun"),
        ("quickjs", "qjs"),
    ):
        if path := shutil.which(executable):
            return runtime, path
    return None


def youtube_dl_class(module: ModuleType) -> type[Any]:
    """Return yt-dlp's public YoutubeDL class without importing its submodule.

    The supported embedding API is ``yt_dlp.YoutubeDL``.  A long-running Python
    process can occasionally have the package attribute replaced by the already
    imported ``yt_dlp.YoutubeDL`` module.  Resolve that state defensively while
    keeping the normal package import path used by the known-good downloader.
    """
    candidate = getattr(module, "YoutubeDL", None)
    if isinstance(candidate, type):
        return candidate

    nested = getattr(candidate, "YoutubeDL", None)
    if isinstance(nested, type):
        # A previous integration version imported ``yt_dlp.YoutubeDL`` as a
        # submodule. Python then replaces the package attribute with that module,
        # so later ``from yt_dlp import YoutubeDL`` calls can receive a module
        # instead of the public class. Restore the public package API in-place;
        # this also repairs a running HA interpreter after an integration reload.
        setattr(module, "YoutubeDL", nested)
        return nested

    raise RuntimeError("yt-dlp YoutubeDL class is unavailable")

