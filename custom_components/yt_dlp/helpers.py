"""Blocking helpers for the YouTube-DLP integration."""

from __future__ import annotations

import os
import shutil
import tempfile


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

    Path lookups may touch the file system, so this helper is intentionally
    called lazily from yt-dlp executor workers rather than during HA startup.
    """
    for runtime, executable in (
        ("deno", "deno"),
        ("node", "node"),
        ("bun", "bun"),
        ("quickjs", "qjs"),
    ):
        if path := shutil.which(executable):
            return runtime, path
    return None
