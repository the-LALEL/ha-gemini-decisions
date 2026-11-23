"""Constants for Gemini Decisions."""

import logging
from typing import Final

DOMAIN: Final = "gemini_decisions"
LOGGER: Final = logging.getLogger(__package__)

DEFAULT_TITLE: Final = "Gemini Decisions"
GOOGLE_GEMINI_DOMAIN: Final = "google_generative_ai_conversation"

BASE_URL: Final = "https://generativelanguage.googleapis.com/v1beta"

CONF_KEY_SOURCE: Final = "key_source"
KEY_SOURCE_MANUAL: Final = "manual"
CONF_SOURCE_ENTRY_ID: Final = "source_entry_id"

CONF_MODEL: Final = "model"
CONF_RESOLUTION: Final = "image_resolution"
CONF_TIMEOUT: Final = "timeout"
CONF_MAX_PER_MINUTE: Final = "max_requests_per_minute"
CONF_MAX_PER_DAY: Final = "max_requests_per_day"
CONF_ALLOW_ATTACHMENTS: Final = "allow_attachments"
CONF_MAX_ATTACHMENT_MB: Final = "max_attachment_mb"

DEFAULT_MODEL: Final = "gemini-decision-1"
DEFAULT_RESOLUTION: Final = "medium"
DEFAULT_TIMEOUT: Final = 30
DEFAULT_MAX_PER_MINUTE: Final = 12
DEFAULT_MAX_PER_DAY: Final = 300
DEFAULT_ALLOW_ATTACHMENTS: Final = False
DEFAULT_MAX_ATTACHMENT_MB: Final = 20

RESOLUTIONS: Final = ("low", "medium", "high", "ultra_high")
MAX_CONCURRENT: Final = 2
MAX_ATTACHMENTS: Final = 8
# Google's inline limit is 100 MB per request; keep the encoded body below it.
HARD_MAX_ENCODED_BYTES: Final = 40 * 1024 * 1024
HARD_MAX_PDF_BYTES: Final = 50 * 1024 * 1024

MAX_QUESTIONS: Final = 1000
MAX_OPTIONS: Final = 255
MAX_OPTION_CHARS: Final = 128
MAX_OPTION_BYTES: Final = 512
MAX_RETRY_AFTER: Final = 5.0

ISSUE_DAILY_CAP: Final = "daily_cap_reached"

# Exact MIME types the Decisions API accepts, by content block type.
IMAGE_TYPES: Final = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/heic",
        "image/heif",
        "image/gif",
        "image/bmp",
        "image/tiff",
    }
)
AUDIO_TYPES: Final = frozenset(
    {
        "audio/wav",
        "audio/mp3",
        "audio/mpeg",
        "audio/aiff",
        "audio/aac",
        "audio/ogg",
        "audio/flac",
        "audio/m4a",
        "audio/opus",
        "audio/webm",
        "audio/l16",
        "audio/alaw",
        "audio/mulaw",
    }
)
VIDEO_TYPES: Final = frozenset(
    {
        "video/mp4",
        "video/mpeg",
        "video/mov",
        "video/avi",
        "video/webm",
        "video/wmv",
        "video/x-flv",
        "video/3gpp",
    }
)
DOCUMENT_TYPES: Final = frozenset({"application/pdf"})

MIME_ALIASES: Final = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-ms-bmp": "image/bmp",
    "image/x-bmp": "image/bmp",
    "image/tif": "image/tiff",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/mp4": "audio/m4a",
    "audio/x-m4a": "audio/m4a",
    "audio/x-aiff": "audio/aiff",
    "audio/x-flac": "audio/flac",
    "audio/x-aac": "audio/aac",
    "audio/3gpp": "video/3gpp",
    "video/quicktime": "video/mov",
    "video/x-msvideo": "video/avi",
    "video/x-ms-wmv": "video/wmv",
}
GENERIC_TYPES: Final = frozenset(
    {"", "application/octet-stream", "*/*", "image", "image/*", "binary/octet-stream"}
)
