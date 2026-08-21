# YouTube-DLP for Home Assistant

Custom integration for searching YouTube and downloading video/audio with `yt-dlp` from Home Assistant actions.

## Highlights

- `yt_dlp.download` supports video or audio output, selectable quality and final format.
- `yt_dlp.search` returns title, thumbnail, video URL, duration, channel/uploader and other useful metadata.
- Download and search work run outside Home Assistant's event loop.
- `yt-dlp` is imported lazily on the first action through Home Assistant's import helper, so integration setup remains lightweight.
- Download jobs run in the background by default and are concurrency-limited.
- Temporary `.part`/fragment files are isolated under `.yt_dlp_tmp/<job_id>` and cleaned after completion/failure.
- Uses Home Assistant's built-in `ffmpeg` integration and its configured FFmpeg binary.
- The configured download directory and JavaScript runtime are checked lazily only when an action needs them; an unavailable NAS/mount therefore does not block Home Assistant startup.
- Uses `ConfigEntry.runtime_data`, config-entry-only schema, native service response support and Home Assistant thread-safe job scheduling.
- Includes `translations/en.json` and `translations/vi.json` for current custom-integration localization.
- GitHub tag pushes automatically validate the tagged source and create a GitHub Release. No fixed release ZIP is required by HACS.

## HACS configuration

`hacs.json` intentionally contains only:

```json
{
  "name": "Youtube DLP",
  "render_readme": true
}
```

The repository follows HACS' normal integration layout:

```text
custom_components/yt_dlp/...
```

HACS therefore installs the source from the selected GitHub release/tag directly. `zip_release`, `filename`, `content_in_root` and fixed ZIP assets are not used.

## Requirements

The integration declares these Home Assistant-managed Python requirements:

- `yt-dlp==2026.8.4.234419.dev0`
- `yt-dlp-ejs==0.8.0`

The pinned yt-dlp build is an upstream PyPI development/nightly build newer than stable `2026.7.4`. yt-dlp itself currently recommends the nightly channel for regular users because website changes can make stable releases stale between monthly releases.

The integration first lets yt-dlp use its upstream default YouTube clients. Only an actual HTTP 403 triggers controlled fallback attempts, with IPv4 used only on the final retry.

FFmpeg is obtained from Home Assistant's built-in `ffmpeg` integration. A supported JavaScript runtime (Deno, Node, Bun or QuickJS) is detected lazily if one is already present; it is not added as a mandatory package dependency.

## Installation

### HACS

Add this repository as a custom **Integration** repository, install it, then restart Home Assistant.

### Manual

Copy:

```text
custom_components/yt_dlp
```

into:

```text
<config>/custom_components/yt_dlp
```

and restart Home Assistant.

## Configuration

Go to **Settings -> Devices & services -> Add integration -> YouTube-DLP** and choose the absolute folder for completed media files.

The config flow validates that the folder can be created and written. Normal Home Assistant startup does not touch that folder. It is checked again only when a download actually begins.

## Action `yt_dlp.download`

Downloads start as background jobs unless `wait_for_completion` is enabled.

### Video

```yaml
action: yt_dlp.download
data:
  url: "https://www.youtube.com/watch?v=VIDEO_ID"
  media_type: video
  video_quality: "1080"
  video_format: mp4
  overwrite: false
```

Video qualities: `best`, `2160`, `1440`, `1080`, `720`, `480`, `360`.

Final containers: `mp4`, `mkv`, `webm`.

### Audio

```yaml
action: yt_dlp.download
data:
  url: "https://www.youtube.com/watch?v=VIDEO_ID"
  media_type: audio
  audio_format: mp3
  audio_quality: "192"
  overwrite: false
```

Audio formats: `mp3`, `m4a`, `opus`, `flac`, `wav`.

Audio quality targets: `best`, `320`, `256`, `192`, `128`, `96` kbps.

### Wait for completion

```yaml
action: yt_dlp.download
data:
  url: "https://www.youtube.com/watch?v=VIDEO_ID"
  media_type: audio
  audio_format: m4a
  audio_quality: "256"
  wait_for_completion: true
response_variable: download_result
```

Example response after completion:

```yaml
job_id: 0123456789abcdef...
status: completed
media_type: audio
title: Example
filename: Example [VIDEO_ID].m4a
downloaded_bytes: 12345678
total_bytes: 12345678
progress: 100
speed: null
eta: null
final_files:
  - /media/youtube/Example [VIDEO_ID].m4a
error: null
```

With `wait_for_completion: false`, optional response data returns immediately with a job ID and current state while downloading continues in the background.

## Action `yt_dlp.search`

Search is metadata-only and uses flat extraction so it does not download or fully resolve every result.

```yaml
action: yt_dlp.search
data:
  query: "Adele Hello official"
  limit: 10
response_variable: youtube_results
```

Response shape:

```yaml
query: Adele Hello official
requested: 10
count: 10
results:
  - id: VIDEO_ID
    title: Adele - Hello
    url: https://www.youtube.com/watch?v=VIDEO_ID
    thumbnail: https://...
    duration: 295
    duration_string: "4:55"
    channel: Adele
    channel_id: UC...
    uploader: Adele
    uploader_id: AdeleVEVO
    view_count: 123456789
    live_status: not_live
```

`limit` accepts 1-50 results.

## Download progress

For compatibility with existing dashboards/cards, `yt_dlp.downloader` keeps the legacy progress contract:

- state = number of active/queued jobs;
- each active filename is an attribute;
- each filename contains `speed`, `downloaded`, `total`, and `eta`.

Progress is throttled to approximately one update per second. Up to two downloads and two searches run concurrently so repeated automations do not overwhelm Home Assistant's executor/network resources.

## Automatic GitHub Releases without `zip_release`

Because HACS uses the source of the release/tag when `zip_release` is not enabled, the `manifest.json` version must already match the tag **before the tag is pushed**.

Use the included helper:

```bash
./scripts/release.sh v0.2.3
```

It:

1. requires a clean Git working tree;
2. updates `custom_components/yt_dlp/manifest.json` to `0.2.3`;
3. keeps manifest keys in Hassfest order;
4. compiles Python as a quick local check;
5. commits `Release v0.2.3`;
6. creates annotated tag `v0.2.3`.

Then push:

```bash
git push origin main
git push origin v0.2.3
```

`.github/workflows/release.yml` then validates that tag and manifest versions match, runs static checks, Hassfest and HACS validation, and creates GitHub Release `v0.2.3`. No fixed `yt_dlp.zip` asset is created.

If a tag already exists from an earlier failed workflow, **do not move the tag to a different commit**. Fix the source, create a new patch version such as `v0.2.4`, and push that new tag.

## Notes

- Arbitrary raw yt-dlp options are intentionally not exposed through actions. The controlled schema protects output paths, hooks, post-processors and Home Assistant stability.
- Audio bitrate is a conversion target; it cannot recreate quality absent from the source stream.
- Some videos may require authentication or a PO token, and data-center/VPN/IPv6 routes can still be rejected by YouTube. This integration does not bypass access controls.
- YouTube changes frequently. Advance the pinned yt-dlp build only after verifying the package exists and testing it with the current Home Assistant/Python runtime.
