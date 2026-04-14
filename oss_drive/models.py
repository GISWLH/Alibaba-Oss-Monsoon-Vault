from __future__ import annotations

from dataclasses import dataclass, field

from .client import OssObject


@dataclass(slots=True)
class FolderNode:
    path: str
    name: str
    subdirs: set[str] = field(default_factory=set)
    files: list[OssObject] = field(default_factory=list)


class DriveIndex:
    def __init__(self) -> None:
        self.nodes: dict[str, FolderNode] = {"": FolderNode(path="", name="Root")}
        self.records: list[OssObject] = []

    @classmethod
    def from_records(cls, records: list[OssObject]) -> "DriveIndex":
        index = cls()
        index.records = records
        for obj in records:
            parts = [part for part in obj.relative_key.split("/") if part]
            current = ""
            for folder_name in parts[:-1]:
                node = index.nodes.setdefault(current, FolderNode(path=current, name=current.rsplit("/", 1)[-1] or "Root"))
                next_path = folder_name if not current else f"{current}/{folder_name}"
                node.subdirs.add(next_path)
                index.nodes.setdefault(next_path, FolderNode(path=next_path, name=folder_name))
                current = next_path

            target = index.nodes.setdefault(
                obj.parent_path,
                FolderNode(path=obj.parent_path, name=obj.parent_path.rsplit("/", 1)[-1] or "Root"),
            )
            target.files.append(obj)

        for node in index.nodes.values():
            node.files.sort(key=lambda item: item.name.lower())
        return index

    def get_node(self, path: str) -> FolderNode:
        return self.nodes.setdefault(
            path,
            FolderNode(path=path, name=path.rsplit("/", 1)[-1] or "Root"),
        )

    def folder_count(self) -> int:
        return max(len(self.nodes) - 1, 0)

    def total_size(self) -> int:
        return sum(record.size for record in self.records if not record.is_folder_marker)
