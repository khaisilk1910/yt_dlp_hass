# YouTube-DLP for Home Assistant

Custom integration for searching YouTube and downloading video/audio with `yt-dlp` from Home Assistant actions.

## Highlights

- `yt_dlp.download` is the main combined action and groups Video/Audio settings into native collapsible sections.
- `yt_dlp.download_video` and `yt_dlp.download_audio` remain available when you want a form containing only one media type.
- `yt_dlp.get_job` returns the latest retained status/progress for background downloads.
- `yt_dlp.search` returns title, thumbnail, video URL, duration, channel/uploader and other useful metadata.
- Optional completion notifications can be enabled independently for Home Assistant persistent notifications, auto-discovered Companion App mobile targets, and Zalo Bot.
- Download and search work run outside Home Assistant's event loop.
- `yt-dlp` is imported lazily on the first action through Home Assistant's import helper, so integration setup remains lightweight.
- Download jobs run in the background by default and are concurrency-limited.
- Temporary `.part`/fragment files are isolated under `.yt_dlp_tmp/<job_id>` and cleaned after completion/failure.
- Audio downloads use distinct source-stream fallbacks on HTTP 403: M4A/MP4A, then WebM/Opus, then HLS/progressive media before FFmpeg extracts the requested final audio format.
- Uses Home Assistant's built-in `ffmpeg` integration as the binary hint, then lazily resolves executable names such as `ffmpeg` to an absolute path before passing them to yt-dlp.
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

- `yt-dlp==2026.8.19`
- `yt-dlp-ejs==0.8.0`

`2026.8.19` is intentionally restored from the working `v0.2.1_403_fix` baseline instead of pinning the older `2026.8.4...dev0` build used by v0.2.3. The integration lets yt-dlp use its own current YouTube client defaults rather than hard-coding player-client fallbacks.

FFmpeg is obtained from Home Assistant's built-in `ffmpeg` integration. Home Assistant commonly exposes its binary as the executable name `ffmpeg`; before yt-dlp receives `ffmpeg_location`, the name is resolved lazily through `PATH` to a real absolute executable path. This avoids yt-dlp interpreting the literal string `ffmpeg` as a missing relative filesystem path. A supported JavaScript runtime (Deno, Node, Bun or QuickJS) is also detected lazily if one is already present.

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

## Completion notifications

Open **Settings -> Devices & services -> YouTube-DLP -> Configure**. Notification settings are stored in `ConfigEntry.options`, so changing them does not reload the integration or interrupt active downloads.

Three independent sections are available:

- **Home Assistant** - sends through `notify.persistent_notification`, with `persistent_notification.create` as a compatibility fallback.
- **Mobile device** - scans Home Assistant's in-memory service registry every time the options screen opens and lists the currently registered `notify.mobile_app_*` Companion App actions.
- **Zalo** - sends through `zalo_bot.send_message`; configure `Thread ID`, `Zalo account`, and `Type` (`User` -> `0`, `Group` -> `1`). Thread IDs stay strings so large Zalo IDs are not rounded.

Notifications are sent only after a job reaches `completed`. Delivery uses non-blocking Home Assistant action calls; if a phone/Zalo target is unavailable, the download remains successful and the integration only writes a warning to the log.

The completion payload now also exposes useful final metadata such as final format, quality, file size, duration, resolution, channel/uploader and source URL when yt-dlp provides it. The notification formats are optimized per destination: Markdown for Home Assistant and compact emoji-based plain text for mobile/Zalo.

## Download actions

### `yt_dlp.download_video`

Use this action for video. The Home Assistant form only shows video-specific settings.

```yaml
action: yt_dlp.download_video
data:
  url: "https://www.youtube.com/watch?v=VIDEO_ID"
  video_quality: "1080"
  video_format: mp4
  overwrite: false
```

Video qualities: `best`, `2160`, `1440`, `1080`, `720`, `480`, `360`.

Final containers: `mp4`, `mkv`, `webm`.

### `yt_dlp.download_audio`

Use this action for audio. The Home Assistant form only shows audio-specific settings.

```yaml
action: yt_dlp.download_audio
data:
  url: "https://www.youtube.com/watch?v=VIDEO_ID"
  audio_format: mp3
  audio_quality: "192"
  overwrite: false
```

Audio formats: `mp3`, `m4a`, `opus`, `flac`, `wav`.

Audio quality targets: `best`, `320`, `256`, `192`, `128`, `96` kbps.

For YouTube reliability, the requested final audio format is intentionally separated from the source-stream choice. The worker first tries an M4A/MP4A audio-only stream (the same audio family used by MP4 video downloads), then WebM/Opus. If YouTube returns HTTP 403 or the route is unavailable, a final HLS/progressive fallback is tried and FFmpeg extracts/converts only the audio. Fallback attempts use isolated temp directories so a rejected partial DASH fragment is never resumed as a different source. yt-dlp remains on its default current player-client logic; no hard-coded player client or PO-token provider is injected.

### `yt_dlp.download` (combined)

This is the main combined action. It keeps `media_type` plus two native Home Assistant collapsible sections. Because the default media type is `video`, **Video options starts expanded** and **Audio options starts collapsed**. The sections can be opened or collapsed manually in the action editor.

Home Assistant 2026.8 service descriptions support an *initial* `collapsed` state for a section, but the section state cannot currently depend live on another service field. Service field `filter` also depends on selected entity features/attributes rather than another field such as `media_type`. Therefore the integration intentionally does not inject custom frontend JavaScript to force dynamic section switching. Use `download_video` / `download_audio` when you need Home Assistant to show only the applicable controls automatically.

### Response behavior

Downloads still run outside Home Assistant's event loop. If `wait_for_completion: true`, the action waits for the worker and returns a completed result.

```yaml
action: yt_dlp.download_audio
data:
  url: "https://www.youtube.com/watch?v=VIDEO_ID"
  audio_format: m4a
  audio_quality: "256"
  wait_for_completion: true
response_variable: download_result
```

Example completed response:

```yaml
job_id: 0123456789abcdef...
status: completed
media_type: audio
title: Example
filename: Example [VIDEO_ID].m4a
downloaded_bytes: 12345678
total_bytes: 12345678
progress: 100
speed: 1234567.0
eta: 0
final_files:
  - /media/youtube/Example [VIDEO_ID].m4a
file_size_bytes: 12345678
format: m4a
quality: 256 kbps
duration: 295.0
duration_string: "4:55"
resolution: null
width: null
height: null
channel: Example Channel
uploader: Example Channel
source_url: https://www.youtube.com/watch?v=VIDEO_ID
metadata:
  id: VIDEO_ID
  source_url: https://www.youtube.com/watch?v=VIDEO_ID
  duration: 295.0
  duration_string: "4:55"
  channel: Example Channel
  uploader: Example Channel
  format: m4a
  quality: 256 kbps
  resolution: null
  width: null
  height: null
  file_size_bytes: 12345678
error: null
```

With `wait_for_completion: false`, a call that requests response data waits only briefly for the first useful yt-dlp progress metadata instead of immediately returning an all-null `queued` snapshot. The download continues in the background. Calls that do not request response data still return immediately.

### `yt_dlp.get_job`

Use the returned job ID to read the newest retained snapshot later:

```yaml
action: yt_dlp.get_job
data:
  job_id: "0123456789abcdef0123456789abcdef"
response_variable: download_status
```

Finished/error/cancelled jobs are retained in bounded memory (up to 50 finished jobs) so this does not grow without limit.

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
./scripts/release.sh v0.3.0
```

It:

1. requires a clean Git working tree;
2. updates `custom_components/yt_dlp/manifest.json` to `0.3.0`;
3. keeps manifest keys in Hassfest order;
4. compiles Python as a quick local check;
5. commits `Release v0.3.0`;
6. creates annotated tag `v0.3.0`.

Then push:

```bash
git push origin main
git push origin v0.3.0
```

`.github/workflows/release.yml` then validates that tag and manifest versions match, runs static checks, Hassfest and HACS validation, and creates GitHub Release `v0.3.0`. No fixed `yt_dlp.zip` asset is created.

If a tag already exists from an earlier failed workflow, **do not move the tag to a different commit**. Fix the source, create a new patch version such as `v0.3.1`, and push that new tag.

## Notes

- Arbitrary raw yt-dlp options are intentionally not exposed through actions. The controlled schema protects output paths, hooks, post-processors and Home Assistant stability.
- Audio bitrate is a conversion target; it cannot recreate quality absent from the source stream.
- Some videos may require authentication or a PO token, and data-center/VPN/IPv6 routes can still be rejected by YouTube. This integration does not bypass access controls.
- YouTube changes frequently. Advance the pinned yt-dlp build only after verifying the package exists and testing it with the current Home Assistant/Python runtime.
