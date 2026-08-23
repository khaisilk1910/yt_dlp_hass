"""Constants for the YouTube-DLP integration."""

from __future__ import annotations

DOMAIN = "yt_dlp"
VERSION = "0.5.16"

STATE_DOWNLOADER = f"{DOMAIN}.downloader"
STATE_FAVORITES_PLAYBACK = f"{DOMAIN}.favorites_playback"

SERVICE_DOWNLOAD = "download"
SERVICE_DOWNLOAD_VIDEO = "download_video"
SERVICE_DOWNLOAD_AUDIO = "download_audio"
SERVICE_GET_JOB = "get_job"
SERVICE_SEARCH = "search"
SERVICE_PLAY = "play"
SERVICE_PLAY_MULTI = "play_multi"
SERVICE_SCAN_LIBRARY = "scan_library"
SERVICE_FAVORITES_LIST = "favorites_list"
SERVICE_FAVORITES_ADD = "favorites_add"
SERVICE_FAVORITES_REMOVE = "favorites_remove"
SERVICE_FAVORITES_PLAYBACK_GET = "favorites_playback_get"
SERVICE_FAVORITES_PLAYBACK_SET = "favorites_playback_set"
SERVICE_FAVORITES_PLAYBACK_START = "favorites_playback_start"
SERVICE_FAVORITES_PLAYBACK_SKIP = "favorites_playback_skip"
SERVICE_FAVORITES_PLAYBACK_STOP = "favorites_playback_stop"

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
ATTR_MEDIA_PLAYER = "media_player"
ATTR_MEDIA_PLAYERS = "media_players"
ATTR_FORCE = "force"
ATTR_ONLINE_SELECTED = "online_selected"
ATTR_OFFLINE_SELECTED = "offline_selected"
ATTR_REPEAT_MODE = "repeat_mode"
ATTR_KIND = "kind"
ATTR_QUEUE = "queue"
ATTR_REPLACE_SELECTION = "replace_selection"
ATTR_DIRECTION = "direction"

MEDIA_TYPE_VIDEO = "video"
MEDIA_TYPE_AUDIO = "audio"
MEDIA_TYPES = (MEDIA_TYPE_VIDEO, MEDIA_TYPE_AUDIO)

VIDEO_QUALITIES = ("best", "2160", "1440", "1080", "720", "480", "360")
VIDEO_FORMATS = ("mp4", "mkv", "webm")
AUDIO_FORMATS = ("mp3", "m4a", "opus", "flac", "wav")
AUDIO_QUALITIES = ("best", "320", "256", "192", "128", "96")

DEFAULT_MEDIA_TYPE = MEDIA_TYPE_VIDEO
DEFAULT_VIDEO_QUALITY = "best"
DEFAULT_VIDEO_FORMAT = "mp4"
DEFAULT_AUDIO_FORMAT = "mp3"
DEFAULT_AUDIO_QUALITY = "best"
DEFAULT_SEARCH_LIMIT = 10
CONF_MEDIA_LIBRARY_PATH = "media_library_path"
MAX_SEARCH_LIMIT = 50

DEFAULT_FILENAME_TEMPLATE = "%(title).180B [%(id)s].%(ext)s"
TEMP_DIR_NAME = ".yt_dlp_tmp"
CACHE_DIR_NAME = ".yt_dlp_cache"

MAX_CONCURRENT_DOWNLOADS = 2
MAX_CONCURRENT_SEARCHES = 2
MAX_RETAINED_JOBS = 50
RESPONSE_METADATA_TIMEOUT = 15

# Options-flow notification settings. They intentionally live in ConfigEntry.options
# because they change runtime behavior but are not required to set up the integration.
SECTION_FOLDERS = "folders"
SECTION_NOTIFY_HOME_ASSISTANT = "notify_home_assistant"
SECTION_NOTIFY_MOBILE = "notify_mobile"
SECTION_NOTIFY_ZALO = "notify_zalo"

CONF_NOTIFY_ENABLED = "enabled"
CONF_MOBILE_NOTIFY_ACTION = "mobile_notify_action"
CONF_ZALO_THREAD_ID = "thread_id"
CONF_ZALO_ACCOUNT = "account_selection"
CONF_ZALO_TYPE = "type"

ZALO_TYPE_USER = "user"
ZALO_TYPE_GROUP = "group"
ZALO_TYPES = (ZALO_TYPE_USER, ZALO_TYPE_GROUP)
ZALO_SERVICE_DOMAIN = "zalo_bot"
ZALO_SERVICE_SEND_MESSAGE = "send_message"

DEFAULT_NOTIFY_ENABLED = False
