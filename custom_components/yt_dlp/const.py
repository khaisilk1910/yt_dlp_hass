"""Constants for the YouTube-DLP integration."""

from __future__ import annotations

DOMAIN = "yt_dlp"

STATE_DOWNLOADER = f"{DOMAIN}.downloader"

SERVICE_DOWNLOAD = "download"
SERVICE_DOWNLOAD_VIDEO = "download_video"
SERVICE_DOWNLOAD_AUDIO = "download_audio"
SERVICE_GET_JOB = "get_job"
SERVICE_SEARCH = "search"

ATTR_URL = "url"
ATTR_MEDIA_TYPE = "media_type"
ATTR_VIDEO_QUALITY = "video_quality"
ATTR_VIDEO_FORMAT = "video_format"
ATTR_AUDIO_FORMAT = "audio_format"
ATTR_AUDIO_QUALITY = "audio_quality"
ATTR_OVERWRITE = "overwrite"
ATTR_WAIT_FOR_COMPLETION = "wait_for_completion"
ATTR_QUERY = "query"
ATTR_LIMIT = "limit"
ATTR_JOB_ID = "job_id"

MEDIA_TYPE_VIDEO = "video"
MEDIA_TYPE_AUDIO = "audio"
MEDIA_TYPES = (MEDIA_TYPE_VIDEO, MEDIA_TYPE_AUDIO)

VIDEO_QUALITIES = ("best", "2160", "1440", "1080", "720", "480", "360")
VIDEO_FORMATS = ("mp4", "mkv", "webm")
AUDIO_FORMATS = ("mp3", "m4a", "opus", "flac", "wav")
AUDIO_QUALITIES = ("best", "320", "256", "192", "128", "96")

DEFAULT_MEDIA_TYPE = MEDIA_TYPE_VIDEO
DEFAULT_VIDEO_QUALITY = "1080"
DEFAULT_VIDEO_FORMAT = "mp4"
DEFAULT_AUDIO_FORMAT = "mp3"
DEFAULT_AUDIO_QUALITY = "192"
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 50

DEFAULT_FILENAME_TEMPLATE = "%(title).180B [%(id)s].%(ext)s"
TEMP_DIR_NAME = ".yt_dlp_tmp"
CACHE_DIR_NAME = ".yt_dlp_cache"

MAX_CONCURRENT_DOWNLOADS = 2
MAX_CONCURRENT_SEARCHES = 2
MAX_RETAINED_JOBS = 50
RESPONSE_METADATA_TIMEOUT = 15
