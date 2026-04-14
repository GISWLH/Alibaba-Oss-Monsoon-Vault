from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

import oss2

from .config import OssDriveConfig


FOLDER_MARKER_NAME = ".ossdrive_keep"

TEXT_EXTENSIONS = {
    ".csv",
    ".geojson",
    ".html",
    ".ini",
    ".json",
    ".log",
    ".md",
    ".py",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


@dataclass(slots=True)
class OssObject:
    key: str
    relative_key: str
    name: str
    parent_path: str
    extension: str
    size: int
    last_modified: str
    etag: Optional[str]
    is_folder_marker: bool = False

    @property
    def is_text_previewable(self) -> bool:
        return self.extension in TEXT_EXTENSIONS

    @property
    def is_image_previewable(self) -> bool:
        return self.extension in IMAGE_EXTENSIONS


class OssDriveClient:
    def __init__(self, config: OssDriveConfig) -> None:
        self.config = config
        self._bucket: oss2.Bucket | None = None

    @property
    def bucket(self) -> oss2.Bucket:
        if self._bucket is None:
            auth = oss2.AuthV4(self.config.access_key_id, self.config.access_key_secret)
            self._bucket = oss2.Bucket(
                auth,
                self.config.endpoint,
                self.config.bucket_name,
                region=self.config.region,
            )
        return self._bucket

    def list_objects(self) -> list[OssObject]:
        objects: list[OssObject] = []
        prefix = self.config.normalized_prefix
        for item in oss2.ObjectIterator(self.bucket, prefix=prefix):
            if item.key.endswith("/"):
                continue

            relative_key = self.relative_key_from_absolute(item.key)
            if not relative_key:
                continue

            relative_path = PurePosixPath(relative_key)
            parent_path = str(relative_path.parent).replace("\\", "/")
            if parent_path == ".":
                parent_path = ""

            objects.append(
                OssObject(
                    key=item.key,
                    relative_key=relative_key,
                    name=relative_path.name,
                    parent_path=parent_path,
                    extension=relative_path.suffix.lower(),
                    size=int(getattr(item, "size", 0) or 0),
                    last_modified=str(getattr(item, "last_modified", "") or ""),
                    etag=getattr(item, "etag", None),
                    is_folder_marker=relative_path.name == FOLDER_MARKER_NAME,
                )
            )

        objects.sort(key=lambda record: record.relative_key.lower())
        return objects

    def signed_url(self, obj: OssObject) -> str:
        return self.bucket.sign_url("GET", obj.key, self.config.signed_url_ttl_seconds)

    def read_text_preview(self, obj: OssObject, max_bytes: int | None = None) -> tuple[str, bool]:
        max_size = max_bytes or self.config.preview_max_bytes
        stream = self.bucket.get_object(obj.key)
        payload = stream.read(max_size + 1)
        truncated = len(payload) > max_size
        text = payload[:max_size].decode("utf-8", errors="replace")
        return text, truncated

    def download_to(self, obj: OssObject, base_dir: Path) -> Path:
        target_path = base_dir / PurePosixPath(obj.relative_key)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        self.bucket.get_object_to_file(obj.key, str(target_path))
        return target_path

    def cache_file(self, obj: OssObject) -> Path:
        cache_dir = self.config.resolved_cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        return self.download_to(obj, cache_dir)

    def download_relative_file(self, relative_key: str, base_dir: Path) -> Path:
        relative = PurePosixPath(self.normalize_relative_path(relative_key))
        target_path = base_dir / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        self.bucket.get_object_to_file(self.absolute_key(relative_key), str(target_path))
        return target_path

    def download_relative_file_to_path(self, relative_key: str, target_path: Path) -> Path:
        target_path = Path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        self.bucket.get_object_to_file(self.absolute_key(relative_key), str(target_path))
        return target_path

    def download_folder(self, relative_folder: str, base_dir: Path) -> list[Path]:
        folder = self.normalize_folder_path(relative_folder).rstrip("/")
        folder_root = base_dir / PurePosixPath(folder)
        downloaded: list[Path] = []
        source_prefix = self.absolute_folder_prefix(folder)

        for key in self.iter_absolute_keys_under_folder(folder):
            suffix = key[len(source_prefix) :]
            local_path = folder_root / PurePosixPath(suffix)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            self.bucket.get_object_to_file(key, str(local_path))
            downloaded.append(local_path)

        return downloaded

    def create_folder(self, relative_folder: str) -> str:
        folder = self.normalize_folder_path(relative_folder)
        marker_key = self.absolute_key(f"{folder}{FOLDER_MARKER_NAME}")
        self.bucket.put_object(marker_key, b"")
        return folder

    def delete_file(self, relative_key: str) -> None:
        self.bucket.delete_object(self.absolute_key(relative_key))

    def delete_folder(self, relative_folder: str) -> int:
        keys = list(self.iter_absolute_keys_under_folder(relative_folder))
        self._batch_delete(keys)
        return len(keys)

    def delete_entries(self, entries: list[tuple[str, str]]) -> int:
        keys: list[str] = []
        seen: set[str] = set()
        for kind, relative_path in entries:
            if kind == "folder":
                for key in self.iter_absolute_keys_under_folder(relative_path):
                    if key not in seen:
                        seen.add(key)
                        keys.append(key)
            else:
                key = self.absolute_key(relative_path)
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        self._batch_delete(keys)
        return len(keys)

    def copy_file(self, source_relative_key: str, target_relative_key: str) -> str:
        source_key = self.absolute_key(source_relative_key)
        target_key = self.absolute_key(target_relative_key)
        self.bucket.copy_object(self.config.bucket_name, source_key, target_key)
        return target_relative_key

    def copy_folder(self, source_relative_folder: str, target_relative_folder: str) -> str:
        source_folder = self.normalize_folder_path(source_relative_folder)
        target_folder = self.normalize_folder_path(target_relative_folder)
        self.create_folder(target_folder.rstrip("/"))
        source_prefix = self.absolute_folder_prefix(source_folder)
        target_prefix = self.absolute_folder_prefix(target_folder)

        for key in self.iter_absolute_keys_under_folder(source_folder):
            suffix = key[len(source_prefix) :]
            self.bucket.copy_object(self.config.bucket_name, key, target_prefix + suffix)
        return target_folder

    def move_file(self, source_relative_key: str, target_relative_key: str) -> str:
        if self.normalize_relative_path(source_relative_key) == self.normalize_relative_path(target_relative_key):
            return target_relative_key
        self.copy_file(source_relative_key, target_relative_key)
        self.delete_file(source_relative_key)
        return target_relative_key

    def move_folder(self, source_relative_folder: str, target_relative_folder: str) -> str:
        source_folder = self.normalize_folder_path(source_relative_folder)
        target_folder = self.normalize_folder_path(target_relative_folder)
        if source_folder == target_folder:
            return target_folder
        self.copy_folder(source_folder, target_folder)
        self.delete_folder(source_folder)
        return target_folder

    def rename_file(self, source_relative_key: str, target_name: str) -> str:
        source = PurePosixPath(self.normalize_relative_path(source_relative_key))
        target = str(source.with_name(target_name)).replace("\\", "/")
        return self.move_file(str(source), target)

    def rename_folder(self, source_relative_folder: str, target_name: str) -> str:
        source = self.normalize_folder_path(source_relative_folder).rstrip("/")
        parent = source.rsplit("/", 1)[0] if "/" in source else ""
        target = f"{parent}/{target_name}" if parent else target_name
        return self.move_folder(source, target)

    def upload_file_to_relative(self, local_path: Path, target_relative_key: str) -> str:
        target_key = self.absolute_key(target_relative_key)
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        oss2.resumable_upload(self.bucket, target_key, str(local_path))
        return self.normalize_relative_path(target_relative_key)

    def upload_folder_to_relative(self, local_dir: Path, target_relative_folder: str) -> list[str]:
        local_dir = Path(local_dir)
        target_root = self.normalize_folder_path(target_relative_folder).rstrip("/")
        uploaded: list[str] = []
        self.create_folder(target_root)

        for current_root, dirnames, filenames in os.walk(local_dir):
            current_path = Path(current_root)
            rel_root = current_path.relative_to(local_dir)
            remote_root = target_root if str(rel_root) == "." else f"{target_root}/{rel_root.as_posix()}"

            if not dirnames and not filenames:
                self.create_folder(remote_root)

            for dirname in dirnames:
                self.create_folder(f"{remote_root}/{dirname}")

            for filename in filenames:
                local_file = current_path / filename
                remote_file = f"{remote_root}/{filename}"
                self.upload_file_to_relative(local_file, remote_file)
                uploaded.append(remote_file)

        return uploaded

    def folder_has_children(self, relative_folder: str) -> bool:
        prefix = self.absolute_folder_prefix(relative_folder)
        for item in oss2.ObjectIterator(self.bucket, prefix=prefix):
            if not item.key.endswith("/"):
                return True
        return False

    def path_exists(self, kind: str, relative_path: str) -> bool:
        if kind == "folder":
            return self.folder_has_children(relative_path)
        return self.bucket.object_exists(self.absolute_key(relative_path))

    def absolute_key(self, relative_path: str) -> str:
        relative = self.normalize_relative_path(relative_path)
        return f"{self.config.normalized_prefix}{relative}" if relative else self.config.normalized_prefix.rstrip("/")

    def absolute_folder_prefix(self, relative_folder: str) -> str:
        folder = self.normalize_folder_path(relative_folder)
        return f"{self.config.normalized_prefix}{folder}"

    def relative_key_from_absolute(self, absolute_key: str) -> str:
        prefix = self.config.normalized_prefix
        if prefix and absolute_key.startswith(prefix):
            return absolute_key[len(prefix) :]
        return absolute_key

    def normalize_relative_path(self, path: str) -> str:
        return (path or "").strip().replace("\\", "/").strip("/")

    def normalize_folder_path(self, path: str) -> str:
        normalized = self.normalize_relative_path(path)
        return f"{normalized}/" if normalized else ""

    def iter_absolute_keys_under_folder(self, relative_folder: str) -> Iterable[str]:
        prefix = self.absolute_folder_prefix(relative_folder)
        for item in oss2.ObjectIterator(self.bucket, prefix=prefix):
            if item.key.endswith("/"):
                continue
            yield item.key

    def _batch_delete(self, keys: list[str]) -> None:
        if not keys:
            return
        batch_size = 1000
        for start in range(0, len(keys), batch_size):
            batch = keys[start : start + batch_size]
            if len(batch) == 1:
                self.bucket.delete_object(batch[0])
            else:
                self.bucket.batch_delete_objects(batch)
