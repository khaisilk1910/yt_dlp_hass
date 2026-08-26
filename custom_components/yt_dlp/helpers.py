"""Blocking helpers for the YouTube-DLP integration."""

from __future__ import annotations

import os
import shutil
import subprocess
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

    YouTube extraction in current yt-dlp requires an EJS-capable JavaScript
    runtime. Home Assistant does not guarantee a suitable runtime in PATH. The
    integration resolves a compatible Node.js wheel when it is already present;
    v0.5.24 installs that wheel lazily on the first user-triggered YouTube action
    instead of making it a startup requirement. The import and file checks are
    intentionally lazy and never run while Home Assistant registers the entry.

    The wheel-provided Node.js runtime is preferred over PATH entries so an older
    system Node (for example v20, no longer accepted by current yt-dlp EJS)
    cannot silently break both Play and Download.
    """
    try:
        import nodejs_wheel  # type: ignore[import-not-found]

        package_file = getattr(nodejs_wheel, "__file__", None)
        if package_file:
            package_dir = os.path.dirname(os.path.abspath(os.fspath(package_file)))
            if os.name == "nt":
                bundled_node = os.path.join(package_dir, "node.exe")
            else:
                bundled_node = os.path.join(package_dir, "bin", "node")
            if os.path.isfile(bundled_node) and os.access(bundled_node, os.X_OK):
                return "node", bundled_node
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        # Keep system runtimes as a compatibility fallback for development and
        # uncommon platforms where the bundled wheel cannot be used.
        pass

    for runtime, executable in (
        ("deno", "deno"),
        ("node", "node"),
        ("quickjs", "qjs"),
        ("bun", "bun"),
    ):
        path = shutil.which(executable)
        if not path:
            continue
        # yt-dlp's current EJS support requires Node.js 22+. An older Node in
        # Home Assistant's PATH must not prevent installation of the pinned
        # compatible wheel, otherwise Play and Download can both fail later.
        if runtime == "node" and not _node_version_is_supported(path):
            continue
        return runtime, path
    return None


def _node_version_is_supported(path: str) -> bool:
    """Return whether a PATH Node executable satisfies current yt-dlp EJS."""
    try:
        result = subprocess.run(
            [path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    value = result.stdout.strip().lstrip("vV")
    try:
        major = int(value.split(".", 1)[0])
    except (TypeError, ValueError):
        return False
    return major >= 22


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

