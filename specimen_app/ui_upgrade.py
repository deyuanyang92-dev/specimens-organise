"""升级中心 (UpgradeCenterDialog) — D1/D2/D11/D13/D15/D17 主入口。

Single multi-tab dialog reachable from the new top-level 升级 menu:

- 概览       : current version + install_kind + build_info + 主入口动作.
- 检查更新   : reuse UpdateCheckWorker → markdown render of release notes →
              one-click "下载并安排下次启动安装".
- 下载分发   : (T11) 跨平台下载安装包到指定文件夹.
- 导入更新   : (T11) 选本地 zip → probe + sha256 → 安排安装.
- 自动更新   : (T12) 4 档 mode + channel + interval + keep N.
- 历史版本   : 已安装的旧版本列表 + "设为当前".
- 本地打包   : (T13/dev) PyInstaller 重打包 — 现仅 stub.

The dialog deliberately re-uses the existing :class:`UpdateCheckWorker` /
:class:`UpdateDownloadWorker` defined in :mod:`specimen_app.ui` to avoid
duplicating their (already-tested) logic; the import is deferred inside
each method so :mod:`ui` and :mod:`ui_upgrade` can import each other
without creating a top-level cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .app_settings import (
    AUTO_UPDATE_CHANNEL_OPTIONS,
    AUTO_UPDATE_MODE_OPTIONS,
    load_settings,
    save_settings,
)
from .install_kind import (
    installation_kind,
    kind_description,
    upgrade_advice,
)
from .release_manager import list_releases, release_roots
from .updater import (
    UpdateError,
    check_latest_release,
    default_download_root,
    download_assets_for_distribution,
    import_local_zip,
    is_newer,
    probe_zip,
)
from .updater_pending import (
    PendingUpdate,
    clear_pending,
    now_iso,
    read_pending,
    write_pending,
)

if TYPE_CHECKING:
    from .ui import SpecimenWindow

_TAB_KEYS = ("overview", "check", "distribute", "import", "settings", "history", "build")


# --------------------------------------------------------------------------- #
# About dialog (D15 helper)
# --------------------------------------------------------------------------- #


def open_about_dialog(parent) -> None:
    """Show a compact "About 当前版本" message box covering D16 / D15."""
    kind = installation_kind()
    pending = read_pending()
    pending_line = ""
    if pending and not pending.is_stale():
        pending_line = f"\n\n已下载待安装：v{pending.version}（下次启动时安装）"

    QMessageBox.information(
        parent,
        "关于当前版本",
        f"标本入库管理 v{__version__}\n\n"
        f"安装方式：{kind_description(kind)}\n"
        f"{upgrade_advice(kind)}"
        f"{pending_line}",
    )


# --------------------------------------------------------------------------- #
# UpgradeCenterDialog
# --------------------------------------------------------------------------- #


class UpgradeCenterDialog(QDialog):
    """Top-level 升级 dialog. ``initial_tab`` selects which tab is open
    on launch — passed by the slot that invoked us.
    """

    def __init__(self, parent: "SpecimenWindow", initial_tab: str = "overview"):
        super().__init__(parent)
        self.parent_window = parent
        self.setWindowTitle("升级中心")
        self.setMinimumSize(680, 520)
        self.resize(820, 600)
        self._workers: list = []  # keep refs so QThreads don't get GC'd

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self._tab_indices: dict[str, int] = {}
        for key, title, builder in (
            ("overview", "概览", self._build_overview_tab),
            ("check", "检查更新", self._build_check_tab),
            ("distribute", "下载分发", self._build_distribute_tab),
            ("import", "导入更新", self._build_import_tab),
            ("settings", "自动更新", self._build_settings_tab),
            ("history", "历史版本", self._build_history_tab),
            ("build", "本地打包", self._build_build_tab),
        ):
            widget = builder()
            idx = self.tabs.addTab(widget, title)
            self._tab_indices[key] = idx

        if initial_tab in self._tab_indices:
            self.tabs.setCurrentIndex(self._tab_indices[initial_tab])

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    # ------------------------------------------------------------------ #
    # 概览 tab
    # ------------------------------------------------------------------ #

    def _build_overview_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        kind = installation_kind()
        title = QLabel(f"标本入库管理 v{__version__}")
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title.setFont(title_font)
        v.addWidget(title)

        v.addWidget(QLabel(f"安装方式：{kind_description(kind)}"))

        advice = QLabel(upgrade_advice(kind))
        advice.setWordWrap(True)
        advice.setStyleSheet("color: #555;")
        v.addWidget(advice)

        pending = read_pending()
        if pending and not pending.is_stale():
            badge = QLabel(
                f"🟡 已下载待安装：v{pending.version}（下次启动时弹窗确认安装）"
            )
            badge.setStyleSheet("padding: 8px; background: #fff3cd; "
                                 "border: 1px solid #ffeaa7; border-radius: 4px;")
            v.addWidget(badge)

        v.addSpacing(20)
        v.addWidget(QLabel("快速动作："))
        actions_row = QHBoxLayout()
        btn_check = QPushButton("立即检查更新")
        btn_check.clicked.connect(
            lambda: self.tabs.setCurrentIndex(self._tab_indices["check"])
        )
        btn_import = QPushButton("从本地文件安装…")
        btn_import.clicked.connect(
            lambda: self.tabs.setCurrentIndex(self._tab_indices["import"])
        )
        btn_settings = QPushButton("自动更新设置…")
        btn_settings.clicked.connect(
            lambda: self.tabs.setCurrentIndex(self._tab_indices["settings"])
        )
        actions_row.addWidget(btn_check)
        actions_row.addWidget(btn_import)
        actions_row.addWidget(btn_settings)
        actions_row.addStretch(1)
        v.addLayout(actions_row)

        v.addStretch(1)
        return w

    # ------------------------------------------------------------------ #
    # 检查更新 tab
    # ------------------------------------------------------------------ #

    def _build_check_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        self._check_status = QLabel("点击 [立即检查] 查询 GitHub 最新版本…")
        self._check_status.setWordWrap(True)
        v.addWidget(self._check_status)

        btn_row = QHBoxLayout()
        self._check_btn = QPushButton("立即检查")
        self._check_btn.clicked.connect(self._do_check)
        self._install_btn = QPushButton("下载并安排下次启动安装")
        self._install_btn.setEnabled(False)
        self._install_btn.clicked.connect(self._do_download_and_stage)
        btn_row.addWidget(self._check_btn)
        btn_row.addWidget(self._install_btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        self._check_progress = QProgressBar()
        self._check_progress.setVisible(False)
        v.addWidget(self._check_progress)

        v.addWidget(QLabel("更新内容："))
        self._notes_view = QTextBrowser()
        self._notes_view.setOpenExternalLinks(True)
        self._notes_view.setStyleSheet("background: #fafafa;")
        v.addWidget(self._notes_view, stretch=1)

        self._latest_release = None  # populated once a check completes
        return w

    def _do_check(self) -> None:
        from .ui import UpdateCheckWorker
        settings = load_settings()
        channel = settings.auto_update_channel or "stable"
        self._check_btn.setEnabled(False)
        self._install_btn.setEnabled(False)
        self._check_status.setText(
            f"正在查询 GitHub（{channel} channel）…"
        )
        # The existing worker calls check_latest_release with no channel —
        # we want the user's channel honored, so use an inline thread.
        worker = _ChannelCheckWorker(channel=channel, parent=self)
        worker.finished_check.connect(self._on_checked)
        self._workers.append(worker)
        worker.start()

    def _on_checked(self, release, error) -> None:
        self._check_btn.setEnabled(True)
        if error is not None:
            self._check_status.setText(f"⚠️ 检查失败：{error}")
            self._notes_view.setMarkdown("")
            return
        if release is None:
            self._check_status.setText("✅ 当前版本已是该 channel 的最新版本。")
            self._notes_view.setMarkdown("")
            return
        self._latest_release = release
        if not is_newer(release.version, __version__):
            self._check_status.setText(
                f"✅ 已是最新（GitHub 上 v{release.version} ≤ 当前 v{__version__}）。"
            )
            self._install_btn.setEnabled(False)
        else:
            self._check_status.setText(
                f"🟢 发现新版 v{release.version}（当前 v{__version__}）"
            )
            self._install_btn.setEnabled(True)
        self._notes_view.setMarkdown(release.notes or "_（该版本未提供发布说明）_")

    def _do_download_and_stage(self) -> None:
        """Download the new bundle, write pending_update.json, ask to quit.

        v0.8.0 ships the "stage on next launch" path only — the actual
        swap happens in :func:`apply_pending_on_startup` (T13). That keeps
        this dialog free of the QApplication.quit() + detached subprocess
        dance and avoids accidentally bricking a session mid-edit.
        """
        if self._latest_release is None:
            return
        from .ui import UpdateDownloadWorker
        release = self._latest_release
        workspace = getattr(self.parent_window, "workspace_root", None)
        dest_root = default_download_root(workspace) if workspace else Path.cwd() / "releases"

        self._install_btn.setEnabled(False)
        self._check_btn.setEnabled(False)
        self._check_progress.setVisible(True)
        self._check_progress.setValue(0)
        self._check_status.setText(f"正在下载 v{release.version} …")

        local_roots = release_roots(workspace) if workspace else []
        worker = UpdateDownloadWorker(release, dest_root, local_roots, parent=self)
        worker.progress.connect(self._check_progress.setValue)
        worker.finished_download.connect(self._on_downloaded)
        self._workers.append(worker)
        worker.start()

    def _on_downloaded(self, target_dir, incremental, error) -> None:
        self._check_progress.setVisible(False)
        self._check_btn.setEnabled(True)
        if error is not None:
            self._install_btn.setEnabled(True)
            QMessageBox.warning(self, "下载失败", f"下载失败：{error}")
            return
        if target_dir is None:
            self._install_btn.setEnabled(True)
            return
        release = self._latest_release
        # Locate the bundle inside the downloaded version dir.
        bundle_dir, exe_name = _locate_bundle(Path(target_dir))
        if bundle_dir is None or not exe_name:
            QMessageBox.warning(
                self, "下载完成但未识别 bundle",
                f"下载到 {target_dir}，但未在其中找到可执行文件。\n"
                "请手动启动该版本，或在 历史版本 tab 操作。",
            )
            return
        pending = PendingUpdate(
            version=release.version,
            bundle_dir=str(bundle_dir),
            exe_name=exe_name,
            from_version=__version__,
            staged_at=now_iso(),
            incremental=bool(incremental),
            workspace=str(getattr(self.parent_window, "workspace_root", "") or ""),
        )
        write_pending(pending)
        # 一键升级 VSCode 风:下完直接关 dialog 走 swap,不二次弹窗。
        # snapshot 在 _launch_pending_swap 内部强制创建,数据安全。
        # 走主窗口 _auto_swap 倒计时(3 秒,可关窗取消)。
        self._check_status.setText(
            f"✅ v{release.version} 已下载到 {bundle_dir}\n准备重启升级…"
        )
        launcher = getattr(self.parent_window, "_arm_auto_swap_countdown", None)
        if callable(launcher):
            self.close()
            launcher(pending, release)
            return
        # Fallback: 旧路径(主窗口未提供倒计时方法时直接 swap)
        legacy = getattr(self.parent_window, "_launch_pending_swap", None)
        if callable(legacy):
            self.close()
            legacy(pending)
            return
        QMessageBox.warning(
            self, "无法启动升级",
            "主窗口未提供升级入口。请退出软件后手动启动新版本。",
        )

    # ------------------------------------------------------------------ #
    # 下载分发 tab (D5)
    # ------------------------------------------------------------------ #

    def _build_distribute_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(
            "下载 GitHub Release 的安装包到指定文件夹，不安装。\n"
            "用于把 Windows / Linux 安装包通过 U 盘 / 网盘等转给离线机器使用。"
        ))

        platform_box = QGroupBox("目标平台")
        platform_layout = QHBoxLayout(platform_box)
        self._dist_platform_group = QButtonGroup(self)
        self._dist_radio_windows = QRadioButton("Windows")
        self._dist_radio_linux = QRadioButton("Linux")
        self._dist_platform_group.addButton(self._dist_radio_windows)
        self._dist_platform_group.addButton(self._dist_radio_linux)
        import sys as _sys
        if _sys.platform == "win32":
            self._dist_radio_windows.setChecked(True)
        else:
            self._dist_radio_linux.setChecked(True)
        platform_layout.addWidget(self._dist_radio_windows)
        platform_layout.addWidget(self._dist_radio_linux)
        platform_layout.addStretch(1)
        v.addWidget(platform_box)

        content_box = QGroupBox("下载内容")
        content_layout = QVBoxLayout(content_box)
        self._dist_chk_zip = QCheckBox("完整安装包 (setup_v*.zip)")
        self._dist_chk_zip.setChecked(True)
        self._dist_chk_zip.setEnabled(False)  # mandatory
        self._dist_chk_sha = QCheckBox("sha256 完整性校验文件")
        self._dist_chk_sha.setChecked(True)
        self._dist_chk_manifest = QCheckBox("update_manifest_*.json（增量更新清单）")
        self._dist_chk_manifest.setChecked(False)
        content_layout.addWidget(self._dist_chk_zip)
        content_layout.addWidget(self._dist_chk_sha)
        content_layout.addWidget(self._dist_chk_manifest)
        v.addWidget(content_box)

        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("保存到："))
        self._dist_dest_edit = QLineEdit()
        settings = load_settings()
        default_dir = (
            settings.upgrade_last_distribution_dir
            or str(Path.home() / "Downloads")
        )
        self._dist_dest_edit.setText(default_dir)
        dest_row.addWidget(self._dist_dest_edit, stretch=1)
        btn_browse = QPushButton("…")
        btn_browse.clicked.connect(self._dist_pick_dir)
        dest_row.addWidget(btn_browse)
        v.addLayout(dest_row)

        self._dist_progress = QProgressBar()
        self._dist_progress.setVisible(False)
        v.addWidget(self._dist_progress)

        self._dist_status = QLabel("")
        self._dist_status.setWordWrap(True)
        v.addWidget(self._dist_status)

        btn_row = QHBoxLayout()
        self._dist_btn = QPushButton("开始下载")
        self._dist_btn.clicked.connect(self._do_distribute)
        btn_row.addWidget(self._dist_btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        v.addStretch(1)
        return w

    def _dist_pick_dir(self) -> None:
        current = self._dist_dest_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "选择保存目录", current)
        if chosen:
            self._dist_dest_edit.setText(chosen)

    def _do_distribute(self) -> None:
        plat = "windows" if self._dist_radio_windows.isChecked() else "linux"
        dest_dir = self._dist_dest_edit.text().strip()
        if not dest_dir:
            QMessageBox.warning(self, "请选择保存目录", "请填写下载保存目录。")
            return
        dest_path = Path(dest_dir)
        if not dest_path.exists():
            reply = QMessageBox.question(
                self, "目录不存在",
                f"{dest_path} 不存在，是否创建？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            try:
                dest_path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.warning(self, "无法创建目录", str(exc))
                return

        settings = load_settings()
        channel = settings.auto_update_channel or "stable"

        self._dist_btn.setEnabled(False)
        self._dist_progress.setVisible(True)
        self._dist_progress.setValue(0)
        self._dist_status.setText(f"查询 {plat} 平台 {channel} channel 最新版…")

        worker = _DistributionWorker(
            platform_key=plat,
            channel=channel,
            dest_dir=dest_path,
            include_sha256=self._dist_chk_sha.isChecked(),
            include_manifest=self._dist_chk_manifest.isChecked(),
            parent=self,
        )
        worker.progress.connect(self._dist_progress.setValue)
        worker.finished_distribute.connect(self._on_distribute_done)
        self._workers.append(worker)
        worker.start()

    def _on_distribute_done(self, files, error) -> None:
        self._dist_progress.setVisible(False)
        self._dist_btn.setEnabled(True)
        if error is not None:
            self._dist_status.setText(f"⚠️ 下载失败：{error}")
            QMessageBox.warning(self, "下载失败", str(error))
            return
        # Persist the chosen directory so next visit defaults to it.
        try:
            settings = load_settings()
            settings.upgrade_last_distribution_dir = self._dist_dest_edit.text().strip()
            save_settings(settings)
        except Exception:
            pass
        listing = "\n".join(f"  • {p.name}" for p in files)
        self._dist_status.setText(
            f"✅ 共下载 {len(files)} 个文件到 {self._dist_dest_edit.text()}\n{listing}"
        )

    # ------------------------------------------------------------------ #
    # 导入更新 tab (D4)
    # ------------------------------------------------------------------ #

    def _build_import_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(
            "从本地 zip 安装更新（离线场景）。选完整安装包 setup_v*.zip。\n"
            "若 zip 旁边有同名 .sha256 文件会自动验证完整性。"
        ))

        zip_row = QHBoxLayout()
        zip_row.addWidget(QLabel("zip 文件："))
        self._import_zip_edit = QLineEdit()
        self._import_zip_edit.setPlaceholderText("点 [浏览…] 选择 setup_v*.zip")
        self._import_zip_edit.textChanged.connect(self._import_on_zip_changed)
        zip_row.addWidget(self._import_zip_edit, stretch=1)
        btn_pick = QPushButton("浏览…")
        btn_pick.clicked.connect(self._import_pick_zip)
        zip_row.addWidget(btn_pick)
        v.addLayout(zip_row)

        self._import_probe_label = QLabel("（未选择文件）")
        self._import_probe_label.setWordWrap(True)
        self._import_probe_label.setStyleSheet(
            "padding: 8px; background: #fafafa; border: 1px solid #ddd;"
        )
        v.addWidget(self._import_probe_label)

        self._import_progress = QProgressBar()
        self._import_progress.setVisible(False)
        v.addWidget(self._import_progress)

        btn_row = QHBoxLayout()
        self._import_btn = QPushButton("安装并安排下次启动应用")
        self._import_btn.setEnabled(False)
        self._import_btn.clicked.connect(self._do_import_install)
        btn_row.addWidget(self._import_btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        v.addStretch(1)
        self._import_current_probe = None
        self._import_current_sha256 = None
        return w

    def _import_pick_zip(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "选择更新 zip", str(Path.home()),
            "Update zip (*.zip);;All files (*.*)",
        )
        if chosen:
            self._import_zip_edit.setText(chosen)

    def _import_on_zip_changed(self, text: str) -> None:
        text = text.strip()
        self._import_current_probe = None
        self._import_current_sha256 = None
        self._import_btn.setEnabled(False)
        if not text:
            self._import_probe_label.setText("（未选择文件）")
            return
        zip_path = Path(text)
        if not zip_path.is_file():
            self._import_probe_label.setText(f"⚠️ 未找到文件：{text}")
            return
        try:
            probe = probe_zip(zip_path)
        except UpdateError as exc:
            self._import_probe_label.setText(f"⚠️ 无法识别 zip：{exc}")
            return
        sha_path = zip_path.with_suffix(zip_path.suffix + ".sha256")
        sha_note = ""
        if sha_path.is_file():
            sha_note = f"\n已检测到 sha256 校验文件：{sha_path.name}（将自动验证）"
            self._import_current_sha256 = sha_path
        else:
            sha_note = "\n⚠️ 未检测到 .sha256 校验文件，将跳过完整性校验。"

        downgrade_note = ""
        if probe.version and probe.version < __version__:
            downgrade_note = (
                f"\n⚠️ 注意：选择的版本 v{probe.version} 低于当前 v{__version__}。"
            )

        self._import_probe_label.setText(
            f"识别结果：\n"
            f"  类型：{probe.kind}\n"
            f"  版本：v{probe.version or '?'}\n"
            f"  平台：{probe.platform or '?'}\n"
            f"  bundle 目录名：{probe.bundle_dir_name or '?'}"
            f"{sha_note}{downgrade_note}"
        )
        self._import_current_probe = probe
        if probe.kind == "full":
            self._import_btn.setEnabled(True)

    def _do_import_install(self) -> None:
        probe = self._import_current_probe
        if probe is None or probe.kind != "full":
            return
        zip_path = Path(self._import_zip_edit.text().strip())
        if not zip_path.is_file():
            return
        import sys as _sys
        expected = "windows" if _sys.platform == "win32" else "linux"
        if probe.platform and probe.platform != expected:
            reply = QMessageBox.question(
                self, "平台不匹配",
                f"zip 平台是 {probe.platform}，当前系统是 {expected}。\n"
                f"导入后该版本可能无法运行。继续？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        if probe.version and probe.version < __version__:
            reply = QMessageBox.question(
                self, "版本降级",
                f"选择的版本 v{probe.version} 低于当前 v{__version__}。\n"
                f"确定要降级安装吗？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        workspace = getattr(self.parent_window, "workspace_root", None)
        dest_root = default_download_root(workspace) if workspace else Path.cwd() / "releases"

        self._import_btn.setEnabled(False)
        self._import_progress.setVisible(True)
        self._import_progress.setValue(0)

        worker = _LocalImportWorker(
            zip_path=zip_path,
            dest_root=dest_root,
            expected_platform=expected if probe.platform else None,
            sha256_path=self._import_current_sha256,
            parent=self,
        )
        worker.progress.connect(self._import_progress.setValue)
        worker.finished_import.connect(self._on_import_done)
        self._workers.append(worker)
        worker.start()

    def _on_import_done(self, target_dir, probe, error) -> None:
        self._import_progress.setVisible(False)
        if error is not None:
            self._import_btn.setEnabled(True)
            QMessageBox.warning(self, "导入失败", str(error))
            return
        if target_dir is None:
            return
        bundle_dir, exe_name = _locate_bundle(Path(target_dir))
        if bundle_dir is None or not exe_name:
            QMessageBox.warning(
                self, "已解压但未找到 exe",
                f"已解压到 {target_dir}，但未识别其中的可执行文件。\n"
                "请手动检查目录内容。",
            )
            return
        pending = PendingUpdate(
            version=probe.version or "unknown",
            bundle_dir=str(bundle_dir),
            exe_name=exe_name,
            from_version=__version__,
            staged_at=now_iso(),
            incremental=False,
            workspace=str(getattr(self.parent_window, "workspace_root", "") or ""),
        )
        write_pending(pending)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("导入完成")
        box.setText(
            f"✅ v{probe.version or '?'} 已解压到：\n{bundle_dir}\n\n"
            "立即关闭并安装新版？"
        )
        btn_now = box.addButton("立即升级（重启）", QMessageBox.YesRole)
        box.addButton("稍后", QMessageBox.NoRole)
        box.exec_()
        if box.clickedButton() is btn_now:
            launcher = getattr(self.parent_window, "_launch_pending_swap", None)
            if callable(launcher):
                self.close()
                launcher(pending)
                return
        self.tabs.setCurrentIndex(self._tab_indices["overview"])

    # ------------------------------------------------------------------ #
    # 自动更新设置 tab (D3 / D12 / D18)
    # ------------------------------------------------------------------ #

    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(
            "自动检查 / 下载 / 安装 行为。所有模式都仅在<b>启动时</b>切换版本，"
            "会话中绝不自动应用更新（保护数据完整性）。"
        ))

        form = QFormLayout()

        self._set_mode_combo = QComboBox()
        for key, label in AUTO_UPDATE_MODE_OPTIONS.items():
            self._set_mode_combo.addItem(label, key)
        form.addRow("自动升级模式：", self._set_mode_combo)

        self._set_channel_combo = QComboBox()
        for key, label in AUTO_UPDATE_CHANNEL_OPTIONS.items():
            self._set_channel_combo.addItem(label, key)
        form.addRow("更新 channel：", self._set_channel_combo)

        self._set_interval_combo = QComboBox()
        for hours, label in (
            (1, "每 1 小时"),
            (6, "每 6 小时"),
            (12, "每 12 小时"),
            (24, "每 24 小时（推荐）"),
            (24 * 7, "每周"),
            (24 * 30, "每月"),
        ):
            self._set_interval_combo.addItem(label, hours)
        form.addRow("检查间隔：", self._set_interval_combo)

        self._set_keep_combo = QComboBox()
        for n in range(1, 6):
            self._set_keep_combo.addItem(f"保留最近 {n} 个版本", n)
        form.addRow("历史版本保留：", self._set_keep_combo)

        v.addLayout(form)

        # Pinned / skipped lists shown read-only with a clear button.
        info_box = QGroupBox("已跳过的版本（D12 Sparkle 风）")
        info_layout = QVBoxLayout(info_box)
        self._set_skipped_label = QLabel("（无）")
        self._set_skipped_label.setWordWrap(True)
        info_layout.addWidget(self._set_skipped_label)
        btn_clear_skipped = QPushButton("清除跳过列表")
        btn_clear_skipped.clicked.connect(self._clear_skipped_versions)
        info_layout.addWidget(btn_clear_skipped, alignment=Qt.AlignLeft)
        v.addWidget(info_box)

        v.addStretch(1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self._save_settings_tab)
        button_row.addWidget(save_btn)
        v.addLayout(button_row)

        self._load_settings_into_form()
        return w

    def _load_settings_into_form(self) -> None:
        settings = load_settings()
        mode = settings.auto_update_mode if settings.auto_update_mode in AUTO_UPDATE_MODE_OPTIONS else "off"
        idx = self._set_mode_combo.findData(mode)
        if idx >= 0:
            self._set_mode_combo.setCurrentIndex(idx)
        channel = settings.auto_update_channel or "stable"
        idx = self._set_channel_combo.findData(channel)
        if idx >= 0:
            self._set_channel_combo.setCurrentIndex(idx)
        idx = self._set_interval_combo.findData(int(settings.auto_update_interval_hours))
        if idx >= 0:
            self._set_interval_combo.setCurrentIndex(idx)
        else:
            # Closest match by clamping.
            self._set_interval_combo.setCurrentIndex(3)  # 24 hours default
        idx = self._set_keep_combo.findData(int(settings.auto_update_keep_versions))
        if idx >= 0:
            self._set_keep_combo.setCurrentIndex(idx)

        skipped = settings.auto_update_skipped_versions or []
        self._set_skipped_label.setText(
            "、".join(f"v{v}" for v in skipped) if skipped else "（无）"
        )

    def _save_settings_tab(self) -> None:
        settings = load_settings()
        settings.auto_update_mode = self._set_mode_combo.currentData() or "off"
        settings.auto_update_channel = self._set_channel_combo.currentData() or "stable"
        settings.auto_update_interval_hours = int(self._set_interval_combo.currentData() or 24)
        settings.auto_update_keep_versions = int(self._set_keep_combo.currentData() or 2)
        save_settings(settings)
        QMessageBox.information(self, "已保存", "自动更新设置已保存。")

    def _clear_skipped_versions(self) -> None:
        settings = load_settings()
        if not settings.auto_update_skipped_versions:
            return
        settings.auto_update_skipped_versions = []
        save_settings(settings)
        self._set_skipped_label.setText("（无）")
        QMessageBox.information(self, "已清除", "跳过版本列表已清空。")

    # ------------------------------------------------------------------ #
    # 历史版本 tab
    # ------------------------------------------------------------------ #

    def _build_history_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("已安装的版本（含当前版本）："))
        self._history_table = QTableWidget(0, 3)
        self._history_table.setHorizontalHeaderLabels(["版本", "可执行文件", "目录"])
        self._history_table.horizontalHeader().setStretchLastSection(True)
        self._history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._history_table.setSelectionBehavior(QTableWidget.SelectRows)
        v.addWidget(self._history_table, stretch=1)

        btn_row = QHBoxLayout()
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self._refresh_history)
        btn_open = QPushButton("打开所选目录")
        btn_open.clicked.connect(self._open_selected_release_dir)
        btn_launch = QPushButton("启动选中版本")
        btn_launch.clicked.connect(self._launch_selected_release)
        btn_row.addWidget(btn_refresh)
        btn_row.addWidget(btn_open)
        btn_row.addWidget(btn_launch)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        self._refresh_history()
        return w

    def _refresh_history(self) -> None:
        workspace = getattr(self.parent_window, "workspace_root", None)
        releases = list_releases(workspace) if workspace else []
        self._history_table.setRowCount(len(releases))
        for row, info in enumerate(releases):
            self._history_table.setItem(row, 0, QTableWidgetItem(info.version))
            self._history_table.setItem(
                row, 1, QTableWidgetItem(info.exe_path.name if info.exe_path else "(未找到)")
            )
            item_dir = QTableWidgetItem(str(info.directory))
            item_dir.setData(Qt.UserRole, info)
            self._history_table.setItem(row, 2, item_dir)

    def _selected_release(self):
        row = self._history_table.currentRow()
        if row < 0:
            return None
        item = self._history_table.item(row, 2)
        return item.data(Qt.UserRole) if item else None

    def _open_selected_release_dir(self) -> None:
        info = self._selected_release()
        if info is None:
            return
        # Reuse main window's _open_path helper to honor the platform default.
        opener = getattr(self.parent_window, "_open_path", None)
        if callable(opener):
            opener(info.directory)
        else:
            QMessageBox.information(self, "目录", str(info.directory))

    def _launch_selected_release(self) -> None:
        info = self._selected_release()
        if info is None or info.exe_path is None:
            return
        # Delegate to the main window's _launch_release if it exists; else
        # do a basic Popen.
        launcher = getattr(self.parent_window, "_launch_release", None)
        if callable(launcher):
            launcher(info)
        else:
            import subprocess
            subprocess.Popen([str(info.exe_path)])

    # ------------------------------------------------------------------ #
    # 本地打包 tab — implemented in v0.8.1
    # ------------------------------------------------------------------ #

    def _build_build_tab(self) -> QWidget:
        return _placeholder_tab(
            "本地重新打包并安装（D6）",
            "(开发者模式) 运行 PyInstaller 重打包 + stderr 自动诊断 + 自动安装。\n\n"
            "实现延后到 v0.8.1。",
        )


# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #


def _placeholder_tab(title: str, body: str) -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    lbl_title = QLabel(title)
    f = QFont()
    f.setPointSize(12)
    f.setBold(True)
    lbl_title.setFont(f)
    v.addWidget(lbl_title)
    lbl_body = QLabel(body)
    lbl_body.setWordWrap(True)
    lbl_body.setStyleSheet("color: #555;")
    v.addWidget(lbl_body)
    v.addStretch(1)
    return w


def _locate_bundle(version_dir: Path) -> tuple[Path | None, str]:
    """Find the ``标本入库管理_v*/`` bundle directory and exe filename
    inside a downloaded version directory.
    """
    from .release_manager import _find_executable  # type: ignore[attr-defined]
    if not version_dir.is_dir():
        return None, ""
    # Direct exe at top level (rare path) — version_dir IS the bundle.
    exe = _find_executable(version_dir)
    if exe is None:
        return None, ""
    return exe.parent, exe.name


# --------------------------------------------------------------------------- #
# Channel-aware check worker (delays import to break ui ↔ ui_upgrade cycle)
# --------------------------------------------------------------------------- #


from PyQt5.QtCore import QThread, pyqtSignal


class _ChannelCheckWorker(QThread):
    finished_check = pyqtSignal(object, object)

    def __init__(self, channel: str = "stable", parent=None):
        super().__init__(parent)
        self._channel = channel

    def run(self) -> None:
        from .updater import check_latest_release
        try:
            release = check_latest_release(channel=self._channel)
            self.finished_check.emit(release, None)
        except Exception as exc:
            self.finished_check.emit(None, exc)


class _DistributionWorker(QThread):
    progress = pyqtSignal(int)
    # list[Path] | None, Exception | None
    finished_distribute = pyqtSignal(object, object)

    def __init__(self, *, platform_key: str, channel: str, dest_dir: Path,
                  include_sha256: bool, include_manifest: bool, parent=None):
        super().__init__(parent)
        self._platform = platform_key
        self._channel = channel
        self._dest_dir = dest_dir
        self._include_sha256 = include_sha256
        self._include_manifest = include_manifest

    def run(self) -> None:
        try:
            release = check_latest_release(
                platform_override=self._platform, channel=self._channel,
            )
            if release is None:
                self.finished_distribute.emit(None, UpdateError(
                    "该 channel 下未找到可用 release。"
                ))
                return
            files = download_assets_for_distribution(
                release, self._dest_dir,
                include_sha256=self._include_sha256,
                include_manifest=self._include_manifest,
                progress_cb=self.progress.emit,
            )
            self.finished_distribute.emit(files, None)
        except Exception as exc:
            self.finished_distribute.emit(None, exc)


class _LocalImportWorker(QThread):
    progress = pyqtSignal(int)
    # Path | None, ZipProbe | None, Exception | None
    finished_import = pyqtSignal(object, object, object)

    def __init__(self, *, zip_path: Path, dest_root: Path,
                  expected_platform: str | None,
                  sha256_path: Path | None, parent=None):
        super().__init__(parent)
        self._zip = zip_path
        self._dest_root = dest_root
        self._expected_platform = expected_platform
        self._sha256 = sha256_path

    def run(self) -> None:
        try:
            target, probe = import_local_zip(
                self._zip, self._dest_root,
                expected_platform=self._expected_platform,
                sha256_path=self._sha256,
                progress_cb=self.progress.emit,
            )
            self.finished_import.emit(target, probe, None)
        except Exception as exc:
            self.finished_import.emit(None, None, exc)
