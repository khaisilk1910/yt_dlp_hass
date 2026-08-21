# YouTube-DLP for Home Assistant

Custom integration for searching YouTube and downloading media with `yt-dlp` from Home Assistant actions.

## Main features

- Downloads run outside the Home Assistant event loop.
- The `yt_dlp.download` action starts in the background by default, so automations and Home Assistant startup remain responsive.
- Choose **video** or **audio** in the action UI.
- Video quality: best, 2160p, 1440p, 1080p, 720p, 480p, 360p.
- Video container: MP4, MKV, WebM.
- Audio format: MP3, M4A, Opus, FLAC, WAV.
- Audio quality: best or a target bitrate.
- Temporary `.part`/fragment files are isolated in `.yt_dlp_tmp/<job_id>` and removed when a job finishes or fails. The configured download folder receives the final media file.
- Video output is remuxed to the selected container with FFmpeg when needed, without re-encoding the video stream.
- `yt_dlp.search` returns structured YouTube search results for use in scripts/automations.
- `yt_dlp.downloader` keeps the original active-download progress attribute format for compatibility with existing dashboards/cards.

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

Go to **Settings → Devices & services → Add integration → YouTube-DLP** and enter the absolute folder where completed files should be stored.

Home Assistant OS and Home Assistant Container already include FFmpeg. A Home Assistant Core installation must provide `ffmpeg` and `ffprobe` in `PATH`.

## Action `yt_dlp.download`

The action starts a background job unless `wait_for_completion` is enabled.

### Video example

```yaml
action: yt_dlp.download
data:
  url: "https://www.youtube.com/watch?v=VIDEO_ID"
  media_type: video
  video_quality: "1080"
  video_format: mp4
  overwrite: false
```

### Audio example

```yaml
action: yt_dlp.download
data:
  url: "https://www.youtube.com/watch?v=VIDEO_ID"
  media_type: audio
  audio_format: mp3
  audio_quality: "192"
  overwrite: false
```

### Wait for the final file and capture the response

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

A completed response contains fields such as:

```yaml
job_id: 0123456789abcdef...
status: completed
media_type: audio
title: Example
filename: Example [VIDEO_ID].m4a
progress: 100
final_files:
  - /media/youtube/Example [VIDEO_ID].m4a
error: null
```

With `wait_for_completion: false`, the optional response is returned immediately with a `job_id` and a queued/running status.

## Action `yt_dlp.search`

Search is metadata-only and uses flat extraction so it does not resolve/download every result.

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

`limit` accepts 1–50 results.

## Download progress

The compatibility state `yt_dlp.downloader` preserves the original contract:

- state: number of active/queued jobs;
- each attribute key is an active filename;
- each filename contains `speed`, `downloaded`, `total`, and `eta`.

Detailed status, final paths, and errors are returned by the `yt_dlp.download` action when response data is requested. Progress updates are throttled to avoid flooding the Home Assistant event loop. Downloads are also concurrency-limited so multiple automation calls do not exhaust the worker pool.

## YouTube JavaScript runtime note

Modern `yt-dlp` requires both `yt-dlp-ejs` and a supported external JavaScript runtime for full YouTube format availability. This integration installs the matching `yt-dlp-ejs` package and automatically detects Deno, Node, Bun or QuickJS if one is already available on the host/container.

A JavaScript runtime is intentionally **not** a mandatory Python requirement because the official Python Deno package is not compatible with Alpine/musl-based Home Assistant OS/Container environments. Without a runtime, `yt-dlp` can still work in degraded mode, but YouTube may expose fewer formats. The detected runtime is logged when the integration loads; it is not added to the compatibility progress attributes so older dashboard cards continue to work.

## Notes

- The integration deliberately does not accept arbitrary raw `yt-dlp` options from the action. Keeping a controlled schema prevents callers from overriding paths/hooks/post-processors and avoids inconsistent temporary/final file handling.
- Audio bitrate is a target for FFmpeg conversion; it cannot restore quality that is not present in the source stream.
- YouTube can change its anti-bot/challenge behavior independently of Home Assistant. Keep this integration and its pinned `yt-dlp` dependency updated when new releases are published.
