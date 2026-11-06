# Compatibility validation for 0.5.34

## Local checks performed

- Python syntax compilation of `custom_components/yt_dlp` (`compileall`).
- Python unit tests in `tests/test_marketplace_compat.py` (8 tests): Lovelace resource loading/updating/deduplication, YAML/missing Lovelace fallback, diagnostics data sanitization, DLNA cache executor calls, repository layout, startup absence of awaits.
- Node.js syntax check for the Lovelace card and `tests/test_card.cjs` to check card registration/editor, opacity normalization, invalid and delayed duration/progress handling.
- Parse all repository JSON and YAML, check integration manifest key order, version matching and translation sets.
- Verify no committed pycache generated files and ZIP file integrity after packaging.

## Checks requiring a live Home Assistant instance

- Real Home Assistant 2026.11.0 integration setup, config-flow/options-flow, reload/unload and the HACS/Marketplace catalogue distribution path.
- Hassfest/HACS GitHub Actions (run on push after release) and possible upstream policy updates.
- Actual YouTube search, source extraction, streaming, playback, DLNA devices, notifications, Favorites persistence, and browser-specific UI interaction.
- Collecting true startup timing metrics on a Home Assistant host with installed FFmpeg and appropriate media mounts. The local review only checks there are no awaited I/O operations in `async_setup_entry` and that cache checks use the executor.

No live device or Home Assistant installation was available for these checks; local test pass is not a guarantee of runtime behaviour on all installations.
