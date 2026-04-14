from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .client import FOLDER_MARKER_NAME, OssDriveClient
from .config import OssDriveConfig, app_root, bundle_root
from .models import DriveIndex


def human_size(size: int) -> str:
    if size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


@dataclass(slots=True)
class ClipboardEntry:
    kind: str
    path: str


@dataclass(slots=True)
class ClipboardState:
    mode: str
    entries: list[ClipboardEntry]


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)


class FunctionWorker(QRunnable):
    def __init__(self, fn, *args, **kwargs) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:
            self.signals.error.emit(str(exc))
            return
        self.signals.result.emit(result)


class FileTableWidget(QTableWidget):
    paths_dropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        if not event.mimeData().hasUrls():
            super().dropEvent(event)
            return
        local_paths: list[str] = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                local_paths.append(url.toLocalFile())
        if local_paths:
            self.paths_dropped.emit(local_paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class OssDriveWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config = OssDriveConfig.load()
        self.client = OssDriveClient(self.config)
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(4)
        self.active_workers: list[FunctionWorker] = []

        self.index = DriveIndex()
        self.current_folder = ""
        self.current_selection: list[dict] = []
        self.clipboard_state: ClipboardState | None = None
        self.restore_folder_after_refresh = ""
        self.operation_in_progress = False

        self.setWindowTitle(self.config.app_title)
        self.setMinimumSize(1360, 860)
        self.resize(1540, 960)
        self.setWindowIcon(self._load_icon())
        self._build_ui()
        self._apply_styles()
        self.refresh_objects(preserve_current_folder=False)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 18, 18, 18)
        root_layout.setSpacing(16)
        root_layout.addWidget(self._build_header())
        root_layout.addWidget(self._build_browser_panel(), 1)
        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("准备连接 OSS...")
        self._build_actions()

    def _build_header(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("HeaderFrame")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(18)

        badge = QLabel("OSS")
        badge.setObjectName("BrandBadge")
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedSize(72, 72)
        layout.addWidget(badge, 0, Qt.AlignTop)

        title_block = QVBoxLayout()
        title = QLabel(self.config.app_title)
        title.setObjectName("TitleLabel")
        subtitle = QLabel("单窗格文件管理器。支持复制路径、复制/剪切/粘贴、删除、上传和拖拽上传。")
        subtitle.setObjectName("SubtitleLabel")
        self.bucket_chip = QLabel(f"{self.config.bucket_name}  |  {self.config.normalized_prefix or '/'}")
        self.bucket_chip.setObjectName("BucketChip")
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        title_block.addWidget(self.bucket_chip, 0, Qt.AlignLeft)
        layout.addLayout(title_block, 1)

        summary_block = QVBoxLayout()
        self.summary_label = QLabel("等待索引...")
        self.summary_label.setObjectName("SummaryLabel")
        self.clipboard_label = QLabel("剪贴板：空")
        self.clipboard_label.setObjectName("SummaryLabel")
        summary_block.addWidget(self.summary_label, 0, Qt.AlignRight)
        summary_block.addWidget(self.clipboard_label, 0, Qt.AlignRight)
        layout.addLayout(summary_block)
        return frame

    def _build_browser_panel(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("Panel")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        toolbar_row_1 = QHBoxLayout()
        toolbar_row_1.setSpacing(10)
        self.back_button = QPushButton("返回上级")
        self.back_button.clicked.connect(self.go_up)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh_objects)
        self.new_folder_button = QPushButton("新建文件夹")
        self.new_folder_button.clicked.connect(self.create_folder)
        self.upload_files_button = QPushButton("上传文件")
        self.upload_files_button.clicked.connect(self.upload_files)
        self.upload_folder_button = QPushButton("上传文件夹")
        self.upload_folder_button.clicked.connect(self.upload_folder)
        self.paste_button = QPushButton("粘贴")
        self.paste_button.clicked.connect(self.paste_entries)
        self.open_downloads_button = QPushButton("打开下载目录")
        self.open_downloads_button.clicked.connect(self.open_download_dir)
        for widget in (
            self.back_button,
            self.refresh_button,
            self.new_folder_button,
            self.upload_files_button,
            self.upload_folder_button,
            self.paste_button,
            self.open_downloads_button,
        ):
            toolbar_row_1.addWidget(widget)
        toolbar_row_1.addStretch(1)
        layout.addLayout(toolbar_row_1)

        toolbar_row_2 = QHBoxLayout()
        toolbar_row_2.setSpacing(10)
        self.download_button = QPushButton("下载选中")
        self.download_button.clicked.connect(self.download_selected_entries)
        self.rename_button = QPushButton("重命名")
        self.rename_button.clicked.connect(self.rename_selected_entry)
        self.delete_button = QPushButton("删除")
        self.delete_button.clicked.connect(self.delete_selected_entries)
        self.copy_path_button = QPushButton("复制路径")
        self.copy_path_button.clicked.connect(self.copy_selected_paths_to_clipboard)
        self.copy_button = QPushButton("复制")
        self.copy_button.clicked.connect(self.stage_copy)
        self.cut_button = QPushButton("剪切")
        self.cut_button.clicked.connect(self.stage_cut)
        self.path_label = QLabel("/")
        self.path_label.setObjectName("PathLabel")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索当前目录下的文件夹或文件...")
        self.search_input.textChanged.connect(self.refresh_table)
        for widget in (
            self.download_button,
            self.rename_button,
            self.delete_button,
            self.copy_path_button,
            self.copy_button,
            self.cut_button,
        ):
            toolbar_row_2.addWidget(widget)
        toolbar_row_2.addWidget(self.path_label, 1)
        toolbar_row_2.addWidget(self.search_input, 1)
        layout.addLayout(toolbar_row_2)

        info_row = QHBoxLayout()
        info_row.setSpacing(12)
        self.folder_stats = QLabel("请选择目录")
        self.folder_stats.setObjectName("FolderStats")
        self.selection_label = QLabel("未选择文件")
        self.selection_label.setObjectName("FolderStats")
        info_row.addWidget(self.folder_stats, 1)
        info_row.addWidget(self.selection_label, 1)
        layout.addLayout(info_row)

        self.file_table = FileTableWidget()
        self.file_table.setColumnCount(5)
        self.file_table.setHorizontalHeaderLabels(["名称", "类型", "大小", "更新时间", "扩展名"])
        self.file_table.verticalHeader().setVisible(False)
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.file_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.file_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.file_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_table.itemSelectionChanged.connect(self.on_table_selection_changed)
        self.file_table.itemDoubleClicked.connect(self.on_table_item_double_clicked)
        self.file_table.customContextMenuRequested.connect(self.show_context_menu)
        self.file_table.paths_dropped.connect(self.handle_dropped_paths)
        header = self.file_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(self.file_table, 1)
        return frame

    def _build_actions(self) -> None:
        actions: list[tuple[str, str, object]] = [
            ("刷新", "F5", self.refresh_objects),
            ("返回上级", "Alt+Up", self.go_up),
            ("复制", "Ctrl+C", self.stage_copy),
            ("剪切", "Ctrl+X", self.stage_cut),
            ("粘贴", "Ctrl+V", self.paste_entries),
            ("删除", "Delete", self.delete_selected_entries),
            ("复制路径", "Ctrl+Shift+C", self.copy_selected_paths_to_clipboard),
            ("重命名", "F2", self.rename_selected_entry),
            ("新建文件夹", "Ctrl+Shift+N", self.create_folder),
            ("上传文件", "Ctrl+U", self.upload_files),
        ]
        for text, shortcut, handler in actions:
            action = QAction(text, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(handler)
            self.addAction(action)

    def _apply_styles(self) -> None:
        self.setFont(QFont("Bahnschrift", 10))
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #0f1518; color: #ecdec8; }
            #HeaderFrame, #Panel { background: #172126; border: 1px solid #304148; border-radius: 20px; }
            #BrandBadge { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #c98244, stop:1 #f1d3a8); color: #101417; border-radius: 24px; font: 700 22px "Bahnschrift"; }
            #TitleLabel { font: 700 28px "Palatino Linotype"; color: #fff2df; }
            #SubtitleLabel, #SummaryLabel, #FolderStats { color: #b7a58b; font: 11px "Bahnschrift"; }
            #BucketChip { background: rgba(241, 211, 168, 0.08); color: #f1d3a8; border: 1px solid #86613e; border-radius: 12px; padding: 6px 10px; }
            #PathLabel { background: #11181c; border: 1px solid #314147; border-radius: 14px; padding: 10px 14px; color: #f1d3a8; font: 11px "Consolas"; }
            QPushButton { background: #c98244; color: #12181b; border: none; border-radius: 14px; padding: 10px 18px; font: 700 10px "Bahnschrift"; }
            QPushButton:hover { background: #e19a58; }
            QPushButton:disabled { background: #38454a; color: #7d8b90; }
            QLineEdit, QTableWidget { background: #11181c; border: 1px solid #314147; border-radius: 14px; padding: 8px; color: #efe4d1; selection-background-color: #c98244; selection-color: #101417; }
            QHeaderView::section { background: #1f2b31; color: #e4d6c0; border: none; padding: 8px 10px; font: 700 10px "Bahnschrift"; }
            QTableWidget::item { padding: 8px 4px; }
            QTableWidget::item:selected { background: #c98244; color: #101417; }
            QStatusBar { background: #11181c; color: #d5c2a6; }
            """
        )

    def _load_icon(self) -> QIcon:
        for root in (app_root(), bundle_root()):
            png_icon_path = root / "oss_drive" / "assets" / "app_icon.png"
            if png_icon_path.exists():
                return QIcon(str(png_icon_path))
            icon_path = root / "oss_drive" / "assets" / "vault.svg"
            if icon_path.exists():
                return QIcon(str(icon_path))
        return QIcon()

    def run_worker(self, fn, on_result, *args) -> None:
        worker = FunctionWorker(fn, *args)
        self.active_workers.append(worker)
        worker.signals.result.connect(
            lambda result, current=worker: self._handle_worker_result(current, on_result, result)
        )
        worker.signals.error.connect(lambda message, current=worker: self._handle_worker_error(current, message))
        self.thread_pool.start(worker)

    def _handle_worker_result(self, worker: FunctionWorker, on_result, result) -> None:
        if worker in self.active_workers:
            self.active_workers.remove(worker)
        on_result(result)

    def _handle_worker_error(self, worker: FunctionWorker, message: str) -> None:
        if worker in self.active_workers:
            self.active_workers.remove(worker)
        self.operation_in_progress = False
        self.update_action_states()
        self.statusBar().showMessage("操作失败。")
        QMessageBox.critical(self, "操作失败", message)

    def refresh_objects(self, preserve_current_folder: bool = True) -> None:
        if self.operation_in_progress:
            return
        self.refresh_button.setEnabled(False)
        self.restore_folder_after_refresh = self.current_folder if preserve_current_folder else ""
        self.statusBar().showMessage("正在扫描 OSS 目录，请稍候...")
        self.run_worker(self.client.list_objects, self.on_objects_loaded)

    def on_objects_loaded(self, records) -> None:
        self.refresh_button.setEnabled(True)
        self.index = DriveIndex.from_records(records)
        restored = self.restore_folder_after_refresh
        while restored and restored not in self.index.nodes:
            restored = restored.rsplit("/", 1)[0] if "/" in restored else ""
        self.current_folder = restored
        self.current_selection = []
        visible_records = [record for record in records if not record.is_folder_marker]
        self.summary_label.setText(
            f"{len(visible_records)} 个对象  |  {self.index.folder_count()} 个文件夹  |  {human_size(self.index.total_size())}"
        )
        self.statusBar().showMessage("索引已更新。")
        self.refresh_table()
        self.update_clipboard_label()

    def refresh_table(self) -> None:
        query = self.search_input.text().strip().lower()
        node = self.index.get_node(self.current_folder)
        subdirs = sorted(node.subdirs, key=str.lower)
        files = [item for item in node.files if not item.is_folder_marker and item.name != FOLDER_MARKER_NAME]

        rows: list[dict] = []
        for subdir_path in subdirs:
            name = subdir_path.rsplit("/", 1)[-1]
            if query and query not in name.lower():
                continue
            rows.append({"kind": "folder", "name": name, "path": subdir_path, "obj": None})
        for obj in files:
            if query and query not in obj.name.lower():
                continue
            rows.append({"kind": "file", "name": obj.name, "path": obj.relative_key, "obj": obj})

        self.file_table.setRowCount(len(rows))
        self.file_table.clearSelection()
        self.current_selection = []

        folder_icon = self.style().standardIcon(QStyle.SP_DirIcon)
        file_icon = self.style().standardIcon(QStyle.SP_FileIcon)

        for row_index, row in enumerate(rows):
            if row["kind"] == "folder":
                columns = [row["name"], "文件夹", "-", "-", "-"]
                icon = folder_icon
            else:
                obj = row["obj"]
                columns = [row["name"], "文件", human_size(obj.size), obj.last_modified, obj.extension or "-"]
                icon = file_icon

            for col_index, value in enumerate(columns):
                item = QTableWidgetItem(value)
                if col_index == 0:
                    item.setIcon(icon)
                if row["kind"] == "folder":
                    item.setForeground(QColor("#f1d3a8"))
                self.file_table.setItem(row_index, col_index, item)

            self.file_table.item(row_index, 0).setData(Qt.UserRole, row)

        self.path_label.setText(self.display_path(self.current_folder))
        self.folder_stats.setText(
            f"当前目录 {len(subdirs)} 个子目录  |  {len(files)} 个文件  |  当前显示 {len(rows)} 项"
        )
        self.selection_label.setText("未选择文件")
        self.update_action_states()

    def display_path(self, folder_path: str) -> str:
        prefix = self.config.normalized_prefix or ""
        return f"{prefix}{folder_path}" if folder_path else (prefix or "/")

    def selected_entries(self) -> list[dict]:
        rows = sorted({index.row() for index in self.file_table.selectionModel().selectedRows()})
        entries: list[dict] = []
        for row in rows:
            item = self.file_table.item(row, 0)
            if item is None:
                continue
            payload = item.data(Qt.UserRole)
            if payload:
                entries.append(payload)
        return entries

    def on_table_selection_changed(self) -> None:
        self.current_selection = self.selected_entries()
        count = len(self.current_selection)
        if count == 0:
            self.selection_label.setText("未选择文件")
        elif count == 1:
            entry = self.current_selection[0]
            if entry["kind"] == "folder":
                self.selection_label.setText(f"文件夹: {entry['path']}")
            else:
                obj = entry["obj"]
                self.selection_label.setText(f"文件: {obj.relative_key}  |  {human_size(obj.size)}")
        else:
            self.selection_label.setText(f"已选择 {count} 项")
        self.update_action_states()

    def on_table_item_double_clicked(self, item: QTableWidgetItem) -> None:
        payload = self.file_table.item(item.row(), 0).data(Qt.UserRole)
        if not payload:
            return
        if payload["kind"] == "folder":
            self.current_folder = payload["path"]
            self.refresh_table()
            return
        self.download_selected_entries()

    def show_context_menu(self, position) -> None:
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background: #172126; color: #efe4d1; border: 1px solid #304148; }")
        selected = self.current_selection

        open_action = menu.addAction("打开 / 进入")
        open_action.setEnabled(len(selected) == 1)
        open_action.triggered.connect(self.open_selected_entry)

        menu.addSeparator()

        copy_path_action = menu.addAction("复制路径")
        copy_path_action.setEnabled(bool(selected))
        copy_path_action.triggered.connect(self.copy_selected_paths_to_clipboard)

        copy_action = menu.addAction("复制")
        copy_action.setEnabled(bool(selected))
        copy_action.triggered.connect(self.stage_copy)

        cut_action = menu.addAction("剪切")
        cut_action.setEnabled(bool(selected))
        cut_action.triggered.connect(self.stage_cut)

        paste_action = menu.addAction("粘贴")
        paste_action.setEnabled(self.can_paste())
        paste_action.triggered.connect(self.paste_entries)

        rename_action = menu.addAction("重命名")
        rename_action.setEnabled(len(selected) == 1)
        rename_action.triggered.connect(self.rename_selected_entry)

        delete_action = menu.addAction("删除")
        delete_action.setEnabled(bool(selected))
        delete_action.triggered.connect(self.delete_selected_entries)

        menu.addSeparator()

        upload_files_action = menu.addAction("上传文件")
        upload_files_action.triggered.connect(self.upload_files)
        upload_folder_action = menu.addAction("上传文件夹")
        upload_folder_action.triggered.connect(self.upload_folder)
        new_folder_action = menu.addAction("新建文件夹")
        new_folder_action.triggered.connect(self.create_folder)

        download_action = menu.addAction("下载选中")
        download_action.setEnabled(bool(selected))
        download_action.triggered.connect(self.download_selected_entries)

        menu.exec(self.file_table.viewport().mapToGlobal(position))

    def update_action_states(self) -> None:
        has_selection = bool(self.current_selection)
        one_selection = len(self.current_selection) == 1
        self.back_button.setEnabled(bool(self.current_folder) and not self.operation_in_progress)
        self.refresh_button.setEnabled(not self.operation_in_progress)
        self.new_folder_button.setEnabled(not self.operation_in_progress)
        self.upload_files_button.setEnabled(not self.operation_in_progress)
        self.upload_folder_button.setEnabled(not self.operation_in_progress)
        self.paste_button.setEnabled(self.can_paste() and not self.operation_in_progress)
        self.download_button.setEnabled(has_selection and not self.operation_in_progress)
        self.rename_button.setEnabled(one_selection and not self.operation_in_progress)
        self.delete_button.setEnabled(has_selection and not self.operation_in_progress)
        self.copy_path_button.setEnabled(has_selection)
        self.copy_button.setEnabled(has_selection)
        self.cut_button.setEnabled(has_selection and not self.operation_in_progress)

    def update_clipboard_label(self) -> None:
        if not self.clipboard_state or not self.clipboard_state.entries:
            self.clipboard_label.setText("剪贴板：空")
            return
        mode_label = "复制" if self.clipboard_state.mode == "copy" else "剪切"
        self.clipboard_label.setText(f"剪贴板：{mode_label} {len(self.clipboard_state.entries)} 项")
        self.update_action_states()

    def open_selected_entry(self) -> None:
        if len(self.current_selection) != 1:
            return
        entry = self.current_selection[0]
        if entry["kind"] == "folder":
            self.current_folder = entry["path"]
            self.refresh_table()
            return
        self.download_selected_entries()

    def go_up(self) -> None:
        if not self.current_folder or self.operation_in_progress:
            return
        self.current_folder = self.current_folder.rsplit("/", 1)[0] if "/" in self.current_folder else ""
        self.refresh_table()

    def copy_selected_paths_to_clipboard(self) -> None:
        entries = self.current_selection
        if not entries:
            return
        text = "\n".join(self.display_entry_path(entry) for entry in entries)
        QGuiApplication.clipboard().setText(text)
        self.statusBar().showMessage("路径已复制到剪贴板。")

    def display_entry_path(self, entry: dict) -> str:
        prefix = self.config.normalized_prefix or ""
        return f"{prefix}{entry['path']}"

    def stage_copy(self) -> None:
        self.set_remote_clipboard("copy")

    def stage_cut(self) -> None:
        self.set_remote_clipboard("cut")

    def set_remote_clipboard(self, mode: str) -> None:
        if not self.current_selection:
            return
        self.clipboard_state = ClipboardState(
            mode=mode,
            entries=[ClipboardEntry(kind=entry["kind"], path=entry["path"]) for entry in self.current_selection],
        )
        self.update_clipboard_label()
        self.statusBar().showMessage(f"已加入剪贴板：{len(self.clipboard_state.entries)} 项。")

    def can_paste(self) -> bool:
        return bool(self.clipboard_state and self.clipboard_state.entries)

    def paste_entries(self) -> None:
        if not self.can_paste() or self.operation_in_progress:
            return
        assert self.clipboard_state is not None

        plans: list[tuple[str, str, str]] = []
        reserved_names = self.current_folder_names()

        for entry in self.clipboard_state.entries:
            name = PurePosixPath(entry.path).name
            source_parent = self.client.normalize_relative_path(str(PurePosixPath(entry.path).parent))
            if source_parent == ".":
                source_parent = ""
            if self.clipboard_state.mode == "cut" and entry.kind == "folder":
                source_folder = self.client.normalize_relative_path(entry.path)
                target_folder = self.client.normalize_relative_path(self.current_folder)
                if target_folder == source_folder or target_folder.startswith(f"{source_folder}/"):
                    QMessageBox.warning(self, "无法移动", "不能把文件夹移动到自身或其子目录中。")
                    return
            if self.clipboard_state.mode == "cut" and source_parent == self.client.normalize_relative_path(self.current_folder):
                plans.append((entry.kind, entry.path, entry.path))
                continue

            target_name = self.make_unique_name(name, reserved_names, is_file=entry.kind == "file")
            reserved_names.add(target_name)
            target_path = self.join_path(self.current_folder, target_name)
            plans.append((entry.kind, entry.path, target_path))

        self.operation_in_progress = True
        self.update_action_states()
        self.statusBar().showMessage("正在粘贴选中项...")
        self.run_worker(self.execute_paste_plan, self.on_paste_finished, plans, self.clipboard_state.mode)

    def execute_paste_plan(self, plans, mode: str) -> dict:
        completed: list[str] = []
        for kind, source_path, target_path in plans:
            if kind == "folder":
                if mode == "cut":
                    self.client.move_folder(source_path, target_path)
                else:
                    self.client.copy_folder(source_path, target_path)
            else:
                if mode == "cut":
                    self.client.move_file(source_path, target_path)
                else:
                    self.client.copy_file(source_path, target_path)
            completed.append(target_path)
        return {"count": len(completed), "mode": mode}

    def on_paste_finished(self, result: dict) -> None:
        self.operation_in_progress = False
        if self.clipboard_state and self.clipboard_state.mode == "cut":
            self.clipboard_state = None
        self.update_clipboard_label()
        self.statusBar().showMessage(f"粘贴完成，共处理 {result['count']} 项。")
        self.refresh_objects()

    def create_folder(self) -> None:
        if self.operation_in_progress:
            return
        name, accepted = QInputDialog.getText(self, "新建文件夹", "文件夹名称：")
        if not accepted:
            return
        name = name.strip().strip("/\\")
        if not name:
            return
        if name in self.current_folder_names():
            QMessageBox.warning(self, "名称冲突", "当前目录下已经存在同名文件或文件夹。")
            return
        target_folder = self.join_path(self.current_folder, name)
        self.operation_in_progress = True
        self.update_action_states()
        self.statusBar().showMessage(f"正在创建文件夹 {name} ...")
        self.run_worker(self.client.create_folder, self.on_create_folder_finished, target_folder)

    def on_create_folder_finished(self, folder: str) -> None:
        self.operation_in_progress = False
        self.statusBar().showMessage(f"文件夹已创建：{folder}")
        self.refresh_objects()

    def rename_selected_entry(self) -> None:
        if len(self.current_selection) != 1 or self.operation_in_progress:
            return
        entry = self.current_selection[0]
        current_name = entry["name"]
        new_name, accepted = QInputDialog.getText(self, "重命名", "新名称：", text=current_name)
        if not accepted:
            return
        new_name = new_name.strip().strip("/\\")
        if not new_name or new_name == current_name:
            return
        if new_name in (self.current_folder_names() - {current_name}):
            QMessageBox.warning(self, "名称冲突", "当前目录下已经存在同名文件或文件夹。")
            return

        self.operation_in_progress = True
        self.update_action_states()
        self.statusBar().showMessage(f"正在重命名 {current_name} ...")
        if entry["kind"] == "folder":
            self.run_worker(self.client.rename_folder, self.on_rename_finished, entry["path"], new_name)
        else:
            self.run_worker(self.client.rename_file, self.on_rename_finished, entry["path"], new_name)

    def on_rename_finished(self, target: str) -> None:
        self.operation_in_progress = False
        self.statusBar().showMessage(f"重命名完成：{target}")
        self.refresh_objects()

    def delete_selected_entries(self) -> None:
        if not self.current_selection or self.operation_in_progress:
            return
        count = len(self.current_selection)
        text = "确定要删除这 1 项吗？" if count == 1 else f"确定要删除这 {count} 项吗？"
        reply = QMessageBox.question(self, "确认删除", text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        entries = [(entry["kind"], entry["path"]) for entry in self.current_selection]
        self.operation_in_progress = True
        self.update_action_states()
        self.statusBar().showMessage("正在删除选中项...")
        self.run_worker(self.client.delete_entries, self.on_delete_finished, entries)

    def on_delete_finished(self, deleted_count: int) -> None:
        self.operation_in_progress = False
        self.statusBar().showMessage(f"删除完成，移除了 {deleted_count} 个对象。")
        self.refresh_objects()

    def upload_files(self) -> None:
        if self.operation_in_progress:
            return
        files, _ = QFileDialog.getOpenFileNames(self, "选择要上传的文件")
        if not files:
            return
        self.start_upload_from_local_paths([Path(path) for path in files])

    def upload_folder(self) -> None:
        if self.operation_in_progress:
            return
        folder = QFileDialog.getExistingDirectory(self, "选择要上传的文件夹")
        if not folder:
            return
        self.start_upload_from_local_paths([Path(folder)])

    def handle_dropped_paths(self, paths: list[str]) -> None:
        if self.operation_in_progress:
            return
        local_paths = [Path(path) for path in paths if Path(path).exists()]
        if not local_paths:
            return
        self.start_upload_from_local_paths(local_paths)

    def start_upload_from_local_paths(self, local_paths: list[Path]) -> None:
        if not local_paths:
            return
        plans: list[tuple[str, str, str]] = []
        reserved_names = self.current_folder_names()

        for local_path in local_paths:
            name = local_path.name
            unique_name = self.make_unique_name(name, reserved_names, is_file=local_path.is_file())
            reserved_names.add(unique_name)
            target_path = self.join_path(self.current_folder, unique_name)
            plans.append(("file" if local_path.is_file() else "folder", str(local_path), target_path))

        self.operation_in_progress = True
        self.update_action_states()
        self.statusBar().showMessage(f"正在上传 {len(plans)} 个本地项目...")
        self.run_worker(self.execute_upload_plan, self.on_upload_finished, plans)

    def execute_upload_plan(self, plans) -> dict:
        uploaded_files = 0
        created_folders = 0
        for kind, local_path_str, target_path in plans:
            local_path = Path(local_path_str)
            if kind == "file":
                self.client.upload_file_to_relative(local_path, target_path)
                uploaded_files += 1
            else:
                uploaded = self.client.upload_folder_to_relative(local_path, target_path)
                uploaded_files += len(uploaded)
                created_folders += 1
        return {"uploaded_files": uploaded_files, "uploaded_folders": created_folders}

    def on_upload_finished(self, result: dict) -> None:
        self.operation_in_progress = False
        self.statusBar().showMessage(
            f"上传完成：{result['uploaded_files']} 个文件，{result['uploaded_folders']} 个文件夹。"
        )
        self.refresh_objects()

    def download_selected_entries(self) -> None:
        if not self.current_selection or self.operation_in_progress:
            return
        chosen_dir = QFileDialog.getExistingDirectory(
            self,
            "选择下载目录",
            str(self.config.resolved_download_dir),
        )
        if not chosen_dir:
            return

        entries = [(entry["kind"], entry["path"]) for entry in self.current_selection]
        self.operation_in_progress = True
        self.update_action_states()
        self.statusBar().showMessage(f"正在下载 {len(entries)} 个项目...")
        self.run_worker(self.execute_download_plan, self.on_download_finished, entries, Path(chosen_dir))

    def execute_download_plan(self, entries, base_dir: Path) -> dict:
        saved_paths: list[str] = []
        downloaded_files = 0
        downloaded_roots = 0
        single_file_mode = len(entries) == 1 and entries[0][0] == "file"
        for kind, path in entries:
            if kind == "folder":
                downloaded = self.client.download_folder(path, base_dir)
                downloaded_files += len(downloaded)
                downloaded_roots += 1
                if downloaded:
                    saved_paths.append(str((base_dir / PurePosixPath(path)).resolve()))
            else:
                if single_file_mode:
                    target_path = (base_dir / PurePosixPath(path).name).resolve()
                    saved = self.client.download_relative_file_to_path(path, target_path)
                else:
                    saved = self.client.download_relative_file(path, base_dir)
                downloaded_files += 1
                saved_paths.append(str(saved.resolve()))
        return {
            "downloaded_files": downloaded_files,
            "downloaded_roots": downloaded_roots,
            "target": str(base_dir),
            "saved_paths": saved_paths,
            "single_file_mode": single_file_mode,
        }

    def on_download_finished(self, result: dict) -> None:
        self.operation_in_progress = False
        if result.get("single_file_mode") and result.get("saved_paths"):
            final_path = result["saved_paths"][0]
            self.statusBar().showMessage(f"下载完成：{final_path}")
            QMessageBox.information(
                self,
                "下载完成",
                f"文件已保存到：\n{final_path}",
            )
        else:
            preview_lines = result.get("saved_paths", [])[:5]
            preview_text = "\n".join(preview_lines)
            extra_count = max(len(result.get("saved_paths", [])) - len(preview_lines), 0)
            if extra_count:
                preview_text += f"\n... 以及另外 {extra_count} 项"
            self.statusBar().showMessage(
                f"下载完成：{result['downloaded_files']} 个文件，目录为 {result['target']}"
            )
            QMessageBox.information(
                self,
                "下载完成",
                f"已保存 {result['downloaded_files']} 个文件到：\n{result['target']}\n\n{preview_text}",
            )
        self.update_action_states()

    def current_folder_names(self) -> set[str]:
        node = self.index.get_node(self.current_folder)
        names = {path.rsplit("/", 1)[-1] for path in node.subdirs}
        names.update(file.name for file in node.files if not file.is_folder_marker and file.name != FOLDER_MARKER_NAME)
        return names

    def make_unique_name(self, desired_name: str, existing_names: set[str], is_file: bool) -> str:
        if desired_name not in existing_names:
            return desired_name
        if is_file:
            path = PurePosixPath(desired_name)
            stem = path.stem or path.name
            suffix = path.suffix
            candidate = f"{stem} - copy{suffix}"
            index = 2
            while candidate in existing_names:
                candidate = f"{stem} - copy {index}{suffix}"
                index += 1
            return candidate

        candidate = f"{desired_name} - copy"
        index = 2
        while candidate in existing_names:
            candidate = f"{desired_name} - copy {index}"
            index += 1
        return candidate

    def join_path(self, parent: str, name: str) -> str:
        parent = self.client.normalize_relative_path(parent)
        name = name.strip().strip("/\\")
        return f"{parent}/{name}" if parent else name

    def open_download_dir(self) -> None:
        self.config.resolved_download_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.config.resolved_download_dir)))

    def closeEvent(self, event) -> None:  # noqa: N802
        self.thread_pool.waitForDone(2000)
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("OSS Drive Preview")
    window = OssDriveWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
