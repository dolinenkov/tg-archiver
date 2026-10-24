import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import tomllib


CATEGORIES = {"photos", "videos", "voice", "video_notes", "audio", "stickers", "files"}


@dataclass
class Config:
    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8080
    discovery_seconds: int = 3600
    reconcile_seconds: int = 86400
    reconcile_days: int = 7
    max_file_bytes: int = 0
    transcribe_audio: bool = True
    excluded_chats: set[int] = field(default_factory=set)
    media: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(CATEGORIES, True))
    chats: dict[int, dict] = field(default_factory=dict)

    def included(self, chat_id):
        return chat_id not in self.excluded_chats and self.chats.get(chat_id, {}).get("enabled", True)

    def allows(self, chat_id, category, size):
        override = self.chats.get(chat_id, {}).get("media", {})
        limit = self.chats.get(chat_id, {}).get("max_file_bytes", self.max_file_bytes)
        return self.included(chat_id) and override.get(category, self.media[category]) and (not limit or size <= limit)

    def fingerprint(self, chat_id):
        settings = [self.included(chat_id), self.media, self.max_file_bytes, self.chats.get(chat_id)]
        return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def load(path: Path) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(raw) - {"archive", "media", "chats"}:
        raise ValueError("Unknown configuration section")
    archive = raw.get("archive", {})
    if not isinstance(archive, dict) or not isinstance(raw.get("chats", {}), dict):
        raise ValueError("archive and chats must be configuration tables")
    fields = {"data_dir", "host", "port", "discovery_seconds", "reconcile_seconds", "reconcile_days", "max_file_bytes", "excluded_chats", "transcribe_audio"}
    if set(archive) - fields:
        raise ValueError("Unknown archive setting")
    config = Config(**archive)
    if not isinstance(config.data_dir, str | Path):
        raise ValueError("data_dir must be a path string")
    config.data_dir = (path.resolve().parent / config.data_dir).resolve()
    if not isinstance(config.host, str):
        raise ValueError("host must be a string")
    if type(config.transcribe_audio) is not bool:
        raise ValueError("transcribe_audio must be boolean")
    for name in fields - {"data_dir", "host", "excluded_chats", "transcribe_audio"}:
        value = getattr(config, name)
        if type(value) is not int or value < (0 if name == "max_file_bytes" else 1):
            raise ValueError(f"Invalid {name}")
    if config.port > 65535:
        raise ValueError("Invalid port")
    if not isinstance(config.excluded_chats, list | set) or any(type(x) is not int for x in config.excluded_chats):
        raise ValueError("excluded_chats must contain numeric chat IDs")
    config.excluded_chats = set(config.excluded_chats)
    media = raw.get("media", {})
    validate_media(media)
    config.media.update(media)
    for key, value in raw.get("chats", {}).items():
        chat_id = int(key)
        if not chat_id or not isinstance(value, dict):
            raise ValueError("Each nonzero chat ID must have a settings table")
        if set(value) - {"enabled", "media", "max_file_bytes"}:
            raise ValueError(f"Unknown setting for chat {key}")
        if "enabled" in value and type(value["enabled"]) is not bool:
            raise ValueError("enabled must be boolean")
        if "max_file_bytes" in value and (type(value["max_file_bytes"]) is not int or value["max_file_bytes"] < 0):
            raise ValueError("Invalid per-chat size limit")
        validate_media(value.get("media", {}))
        config.chats[chat_id] = value
    return config


def validate_media(media):
    if not isinstance(media, dict) or set(media) - CATEGORIES or any(type(x) is not bool for x in media.values()):
        raise ValueError("Media settings must be known categories with boolean values")


def credentials():
    try:
        api_id, api_hash = int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]
    except (KeyError, ValueError):
        raise ValueError("Set TG_API_ID and TG_API_HASH") from None
    if api_id <= 0 or len(api_hash) != 32 or any(c not in "0123456789abcdefABCDEF" for c in api_hash):
        raise ValueError("Invalid Telegram API credentials")
    return api_id, api_hash
