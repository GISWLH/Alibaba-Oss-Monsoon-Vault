from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path


CONFIG_FILE_NAME = "oss_drive_config.json"


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class OssDriveConfig:
    app_title: str
    brand_tagline: str
    access_key_id: str
    access_key_secret: str
    endpoint: str
    region: str
    bucket_name: str
    start_prefix: str
    signed_url_ttl_seconds: int
    preview_max_bytes: int
    download_dir: str
    cache_dir: str

    @classmethod
    def load(cls, path: Path | None = None) -> "OssDriveConfig":
        config_path = Path(path) if path else cls.default_config_path()
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        return cls(**payload)

    @classmethod
    def default_config_path(cls) -> Path:
        runtime_path = app_root() / CONFIG_FILE_NAME
        if runtime_path.exists():
            return runtime_path
        bundled_path = bundle_root() / CONFIG_FILE_NAME
        if bundled_path.exists():
            return bundled_path
        return runtime_path

    def save(self, path: Path | None = None) -> None:
        config_path = Path(path or app_root() / CONFIG_FILE_NAME)
        config_path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "app_title": self.app_title,
            "brand_tagline": self.brand_tagline,
            "access_key_id": self.access_key_id,
            "access_key_secret": self.access_key_secret,
            "endpoint": self.endpoint,
            "region": self.region,
            "bucket_name": self.bucket_name,
            "start_prefix": self.start_prefix,
            "signed_url_ttl_seconds": self.signed_url_ttl_seconds,
            "preview_max_bytes": self.preview_max_bytes,
            "download_dir": self.download_dir,
            "cache_dir": self.cache_dir,
        }

    @property
    def normalized_prefix(self) -> str:
        prefix = (self.start_prefix or "").strip().replace("\\", "/").lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        return prefix

    @property
    def resolved_cache_dir(self) -> Path:
        return self._resolve_path(self.cache_dir)

    @property
    def resolved_download_dir(self) -> Path:
        return self._resolve_path(self.download_dir)

    def _resolve_path(self, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return app_root() / path
