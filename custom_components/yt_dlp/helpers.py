"""Small blocking helpers that are always called through the HA executor."""

from __future__ import annotations

import os
import shutil
import tempfile


def ensure_writable_directory(path: str) -> str:
    """Create and validate a writable download directory."""
    normalized = os.path.abspath(os.path.expanduser(path.strip()))
    os.makedirs(normalized, mode=0o755, exist_ok=True)
    if not os.path.isdir(normalized):
        raise OSError(f"Not a directory: {normalized}")
    try:
        with tempfile.NamedTemporaryFile(dir=normalized, prefix=".yt_dlp_write_test_"):
            pass
    except OSError as err:
        raise OSError(f"Directory is not writable: {normalized}") from err
    return normalized


def detect_external_tools() -> tuple[str | None, tuple[str, str] | None]:
    """Detect FFmpeg and a JavaScript runtime supported by yt-dlp."""
    ffmpeg = shutil.which("ffmpeg")
    for runtime, executable in (
        ("deno", "deno"),
        ("node", "node"),
        ("bun", "bun"),
        ("quickjs", "qjs"),
    ):
        if path := shutil.which(executable):
            return ffmpeg, (runtime, path)
    return ffmpeg, None
