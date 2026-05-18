"""入库人员管理对话框 (规范化软件设计 2026-05 新增)。

功能:
- 列表展示全部 TeamMember (按 sort_key 排序:钉位→星标→最近用)
- 增 / 删 / 改
- 星标 (0-5) + 钉位
- 搜索框 (姓名 / 拼音 / 角色)
- 导入 / 导出 CSV
- 跟工作区同步 (打开 dialog 时调 sync_on_workspace_open;关闭时若已改 调 save_members)
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .persons_store import (
    TeamMember,
    ROLE_OPTIONS,
    PURPOSE_OPTIONS,
    color_for,
    load_members,
    save_members,
    sort_key,
    sync_on_workspace_open,
)
from .spreadsheet_preview import SpreadsheetPreviewWidget, _fmt_duration
from .widgets_persons import PersonAvatar


def _human_relative_time(iso: str) -> str:
    if not iso:
        return "从未使用"
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return iso
    delta = datetime.now() - dt
    sec = int(delta.total_seconds())
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return f"{sec // 60} 分钟前"
    if sec < 86400:
        return f"{sec // 3600} 小时前"
    if sec < 86400 * 30:
        return f"{sec // 86400} 天前"
    if sec < 86400 * 365:
        return f"{sec // (86400 * 30)} 月前"
    return f"{sec // (86400 * 365)} 年前"


class PersonsManagerDialog(QDialog):
    """入库人员管理对话框 (规范化软件设计 2026-05 Phase 2: 4-Tab + SpreadsheetPreview)。

    Tab 1 人员库:增删改/星标/钉位/csv 导入导出 (Phase 1 原有)
    Tab 2 工作量统计:汇总 8 列 (录入人/任务/录入/编号/照片/时长/首次/末次)
    Tab 3 任务明细:8 列 (开始/结束/录入人/用途/录入量/编号段/时长/备注)
    Tab 4 编号分发:alloc_log 批量领取行直显

    无 store → 后三 Tab 显占位。
    """

    def __init__(self, parent=None, workspace: Optional[Path] = None,
                 store: Optional["object"] = None, initial_tab: int = 0) -> None:
        super().__init__(parent)
        self.setWindowTitle("入库人员管理")
        self.resize(960, 600)
        self._workspace = workspace
        self._store = store
        self._members: list[TeamMember] = []
        self._current: Optional[TeamMember] = None
        self._dirty = False
        self._build_ui()
        self._reload()
        # 加载统计 Tabs (异步触发,首次 show 后)
        if self._store is not None:
            self._reload_stats_tabs()
        # 切到指定 tab (0=人员库,1=工作量统计,2=任务明细,3=编号分发)
        if 0 <= initial_tab < self._tabs.count():
            self._tabs.setCurrentIndex(initial_tab)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        # 顶层 Tab
        self._tabs = QTabWidget()
        outer.addWidget(self._tabs, 1)

        # Tab 1: 人员库
        members_tab = QWidget()
        self._build_members_tab(members_tab)
        self._tabs.addTab(members_tab, "👤 人员库")

        # Tab 2: 工作量统计
        self._stats_widget = SpreadsheetPreviewWidget()
        self._tabs.addTab(self._stats_widget, "📊 工作量统计")

        # Tab 3: 任务明细
        self._detail_widget = SpreadsheetPreviewWidget()
        self._tabs.addTab(self._detail_widget, "📋 任务明细")

        # Tab 4: 编号分发
        self._alloc_widget = SpreadsheetPreviewWidget()
        self._tabs.addTab(self._alloc_widget, "🔢 编号分发")

        # 底部关闭
        foot = QHBoxLayout()
        foot.addStretch()
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        foot.addWidget(btn_close)
        outer.addLayout(foot)

    def _build_members_tab(self, container: QWidget) -> None:
        outer = QVBoxLayout(container)

        # 顶部:搜索 + 操作
        top = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍 按姓名 / 拼音 / 角色 过滤…")
        self._search.textChanged.connect(self._on_search_changed)
        top.addWidget(self._search, 1)
        btn_add = QPushButton("＋ 新增")
        btn_add.clicked.connect(self._on_add)
        btn_import = QPushButton("导入 CSV")
        btn_import.clicked.connect(self._on_import_csv)
        btn_export = QPushButton("导出 CSV")
        btn_export.clicked.connect(self._on_export_csv)
        btn_save = QPushButton("保存")
        btn_save.clicked.connect(self._on_save_and_apply)
        top.addWidget(btn_add)
        top.addWidget(btn_import)
        top.addWidget(btn_export)
        top.addWidget(btn_save)
        outer.addLayout(top)

        # 主体:列表 + 编辑面板
        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter, 1)

        # 左:列表
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list.itemSelectionChanged.connect(self._on_select)
        self._list.itemDoubleClicked.connect(self._on_rename_inline)
        splitter.addWidget(self._list)

        # 右:编辑面板
        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(8, 8, 8, 8)

        # 头像 + 标题
        head = QHBoxLayout()
        self._head_avatar = PersonAvatar("", size=48)
        head.addWidget(self._head_avatar)
        self._head_title = QLabel("(未选中)")
        head_font = self._head_title.font()
        head_font.setPointSize(13)
        head_font.setBold(True)
        self._head_title.setFont(head_font)
        head.addWidget(self._head_title, 1)
        rlay.addLayout(head)

        form = QFormLayout()
        self._name_edit = QLineEdit()
        self._pinyin_edit = QLineEdit()
        self._role_combo = QComboBox()
        for key, label in ROLE_OPTIONS.items():
            self._role_combo.addItem(label, key)
        self._purpose_combo = QComboBox()
        self._purpose_combo.addItem("(无默认)", "")
        for p in PURPOSE_OPTIONS:
            self._purpose_combo.addItem(p, p)
        self._star_spin = QSpinBox()
        self._star_spin.setRange(0, 5)
        self._star_spin.setSuffix(" ★")
        self._pin_btn = QPushButton("📌 钉位")
        self._pin_btn.setCheckable(True)
        self._pin_btn.toggled.connect(self._on_pin_toggled)
        self._note_edit = QTextEdit()
        self._note_edit.setMaximumHeight(60)

        form.addRow("姓名", self._name_edit)
        form.addRow("拼音", self._pinyin_edit)
        form.addRow("角色", self._role_combo)
        form.addRow("默认用途", self._purpose_combo)

        # 星标 + 钉位 一行
        star_row = QHBoxLayout()
        star_row.addWidget(self._star_spin)
        star_row.addWidget(self._pin_btn)
        star_row.addStretch()
        form.addRow("星标 / 钉位", star_row)

        form.addRow("备注", self._note_edit)
        rlay.addLayout(form)

        # 信息显示
        self._info_lbl = QLabel("")
        self._info_lbl.setStyleSheet("color: #888; font-size: 11px;")
        rlay.addWidget(self._info_lbl)

        rlay.addStretch()

        # 删除按钮
        del_row = QHBoxLayout()
        del_row.addStretch()
        self._btn_delete = QPushButton("🗑 删除该人员")
        self._btn_delete.setStyleSheet("color: #c14d4d;")
        self._btn_delete.clicked.connect(self._on_delete)
        del_row.addWidget(self._btn_delete)
        rlay.addLayout(del_row)

        splitter.addWidget(right)
        splitter.setSizes([300, 500])
        # 注:[关闭]/[保存] 已迁到 _build_ui 外层 + 顶部 toolbar,本 tab 内不再加。
        # 编辑字段改动 → 标 dirty
        for w in (self._name_edit, self._pinyin_edit, self._note_edit):
            sig = w.textChanged if hasattr(w, "textChanged") else None
            if sig:
                sig.connect(self._mark_dirty)
        self._role_combo.currentIndexChanged.connect(self._mark_dirty)
        self._purpose_combo.currentIndexChanged.connect(self._mark_dirty)
        self._star_spin.valueChanged.connect(self._mark_dirty)

    # ---- 统计 Tab (Phase 2) ----
    def _reload_stats_tabs(self) -> None:
        """读 alloc_log + specimen + photo,填三 Tab。"""
        if self._store is None:
            placeholder = [["请先在主窗口选择工作区"]]
            self._stats_widget.set_data(["提示"], placeholder)
            self._detail_widget.set_data(["提示"], placeholder)
            self._alloc_widget.set_data(["提示"], placeholder)
            return
        try:
            tasks, allocations, photo_counts, first_seen, last_seen = self._parse_data()
        except Exception as exc:
            err = [[f"读取失败:{exc}"]]
            self._stats_widget.set_data(["错误"], err)
            self._detail_widget.set_data(["错误"], err)
            self._alloc_widget.set_data(["错误"], err)
            return
        self._fill_stats_tab(tasks, allocations, photo_counts, first_seen, last_seen)
        self._fill_detail_tab(tasks, allocations)
        self._fill_alloc_tab(allocations)

    def _parse_data(self) -> tuple:
        """返回 (tasks, allocations, photo_counts_by_person, first_seen, last_seen)。

        - tasks: list[dict] (开始/结束/人员/用途/录入量/时长秒/备注)
        - allocations: list[dict] (alloc_log "批量领取" 行原样)
        - photo_counts: dict[人员 -> int]
        - first_seen: dict[人员 -> ISO 时间字符串] (alloc_log 中最早出现)
        - last_seen: dict[人员 -> ISO 时间字符串]
        """
        from datetime import datetime as _dt
        rows = self._store.read_alloc_log()

        # 任务对
        starts = {r["记录ID"]: r for r in rows if r.get("类型") == "任务开始"}
        ends = [r for r in rows if r.get("类型") == "任务结束"]
        tasks: list[dict] = []
        for end in ends:
            start_id = end.get("关联任务ID", "")
            start = starts.get(start_id)
            if not start:
                continue
            try:
                t0 = _dt.fromisoformat(start["时间"])
                t1 = _dt.fromisoformat(end["时间"])
                duration_sec = max(0, int((t1 - t0).total_seconds()))
            except (ValueError, KeyError):
                duration_sec = 0
            tasks.append({
                "开始时间": start.get("时间", ""),
                "结束时间": end.get("时间", ""),
                "人员": start.get("人员", ""),
                "用途": start.get("用途", ""),
                "备注": start.get("备注", ""),
                "录入量": int(end.get("数量", 0) or 0),
                "时长秒": duration_sec,
                "记录ID": start.get("记录ID", ""),
            })

        # 批量领取
        allocations = [r for r in rows if r.get("类型") == "批量领取"]

        # first/last seen (按 alloc_log 中此人最早/最末出现)
        first_seen: dict[str, str] = {}
        last_seen: dict[str, str] = {}
        for r in rows:
            p = (r.get("人员") or "").strip()
            t = (r.get("时间") or "").strip()
            if not p or not t:
                continue
            if p not in first_seen or t < first_seen[p]:
                first_seen[p] = t
            if p not in last_seen or t > last_seen[p]:
                last_seen[p] = t

        # 整理照片数:join specimen + photo
        photo_counts = self._compute_photo_counts_by_person()

        return tasks, allocations, photo_counts, first_seen, last_seen

    def _compute_photo_counts_by_person(self) -> dict[str, int]:
        """读 specimen 表取 (入库编号 → 信息录入人员) 映射 + photo 表 count by voucher,聚合到人员。"""
        try:
            specimens = self._store.read_rows("specimen")
            photos = self._store.read_rows("photo")
        except Exception:
            return {}
        v2p: dict[str, str] = {}  # voucher → person
        for row in specimens:
            v = (row.get("入库编号*") or "").strip()
            p = (row.get("信息录入人员") or "").strip()
            if v and p:
                v2p[v] = p
        counts: dict[str, int] = {}
        for row in photos:
            v = (row.get("入库编号*") or "").strip()
            p = v2p.get(v, "")
            if not p:
                continue
            counts[p] = counts.get(p, 0) + 1
        return counts

    def _fill_stats_tab(self, tasks, allocations, photo_counts, first_seen, last_seen) -> None:
        from collections import defaultdict
        summary: dict[str, dict] = defaultdict(lambda: {
            "任务次数": 0, "录入标本数": 0, "时长秒": 0, "领取编号数": 0,
        })
        for t in tasks:
            s = summary[t["人员"]]
            s["任务次数"] += 1
            s["录入标本数"] += t["录入量"]
            s["时长秒"] += t["时长秒"]
        for a in allocations:
            p = (a.get("人员") or "").strip() or "(未指定)"
            try:
                qty = int(a.get("数量", 0) or 0)
            except Exception:
                qty = 0
            summary[p]["领取编号数"] += qty
        # 把仅在 photo_counts / first_seen 出现的人也补进 summary
        for p in set(list(photo_counts.keys()) + list(first_seen.keys())):
            summary[p]  # touch
        # 整理顺序:按录入标本数 desc
        ordered = sorted(summary.items(), key=lambda kv: -kv[1]["录入标本数"])
        columns = ["录入人", "任务次数", "录入标本数", "领取编号数",
                   "整理照片数", "累计时长", "首次登录", "末次活跃"]
        rows = []
        for name, s in ordered:
            rows.append([
                name,
                s["任务次数"],
                s["录入标本数"],
                s["领取编号数"],
                photo_counts.get(name, 0),
                _fmt_duration(s["时长秒"]),
                first_seen.get(name, ""),
                last_seen.get(name, ""),
            ])
        self._stats_widget.set_data(columns, rows)

    def _fill_detail_tab(self, tasks, allocations) -> None:
        # 按"关联任务ID"分组 allocations,拼"编号段"
        seg_map: dict[str, list[str]] = {}  # task_id → segments
        for a in allocations:
            task_id = (a.get("关联任务ID") or "").strip()
            if not task_id:
                continue
            seg = self._fmt_voucher_range(a)
            if seg:
                seg_map.setdefault(task_id, []).append(seg)
        columns = ["任务开始时间", "任务结束时间", "录入人", "用途",
                   "录入量", "领取编号段", "时长", "备注"]
        rows = []
        for t in sorted(tasks, key=lambda x: x["开始时间"], reverse=True):
            segs = seg_map.get(t["记录ID"], [])
            rows.append([
                t["开始时间"],
                t["结束时间"],
                t["人员"],
                t["用途"],
                t["录入量"],
                "; ".join(segs),
                _fmt_duration(t["时长秒"]),
                t["备注"],
            ])
        self._detail_widget.set_data(columns, rows)

    def _fill_alloc_tab(self, allocations) -> None:
        columns = ["时间", "人员", "编号系列", "编号起始", "编号结束", "数量", "关联任务ID", "备注"]
        rows = []
        for a in sorted(allocations, key=lambda x: x.get("时间", ""), reverse=True):
            try:
                qty = int(a.get("数量", 0) or 0)
            except Exception:
                qty = 0
            rows.append([
                a.get("时间", ""),
                a.get("人员", ""),
                a.get("编号系列", ""),
                a.get("编号起始", ""),
                a.get("编号结束", ""),
                qty,
                a.get("关联任务ID", ""),
                a.get("备注", ""),
            ])
        self._alloc_widget.set_data(columns, rows)

    @staticmethod
    def _fmt_voucher_range(a: dict) -> str:
        start = (a.get("编号起始") or "").strip()
        end = (a.get("编号结束") or "").strip()
        if start and end:
            return f"{start}-{end}" if start != end else start
        if start:
            return start
        return ""

    # ---- 数据加载 / 同步 ----
    def _reload(self) -> None:
        if self._workspace is not None:
            self._members = sync_on_workspace_open(self._workspace)
        else:
            self._members = load_members()
        self._refresh_list()
        if self._members:
            self._list.setCurrentRow(0)
        else:
            self._render_panel(None)

    def _refresh_list(self) -> None:
        self._list.clear()
        needle = (self._search.text() or "").strip().lower()
        for m in self._members:
            if needle:
                hay = " ".join([m.name, m.pinyin, m.role, m.note]).lower()
                if needle not in hay:
                    continue
            item = QListWidgetItem(self._format_list_item(m))
            item.setData(Qt.UserRole, m.name)
            self._list.addItem(item)

    @staticmethod
    def _format_list_item(m: TeamMember) -> str:
        marks = []
        if m.pinned:
            marks.append("📌")
        if m.starred:
            marks.append("★" * m.starred)
        prefix = " ".join(marks)
        role = ROLE_OPTIONS.get(m.role, m.role)
        when = _human_relative_time(m.last_used_at)
        return f"{prefix}  {m.name}  ({role})  · {when}".strip()

    # ---- 编辑面板渲染 ----
    def _render_panel(self, member: Optional[TeamMember]) -> None:
        self._current = member
        self._block_edit_signals(True)
        try:
            if member is None:
                self._head_avatar.set_person("")
                self._head_title.setText("(未选中)")
                self._name_edit.setText("")
                self._pinyin_edit.setText("")
                self._role_combo.setCurrentIndex(0)
                self._purpose_combo.setCurrentIndex(0)
                self._star_spin.setValue(0)
                self._pin_btn.setChecked(False)
                self._note_edit.setPlainText("")
                self._info_lbl.setText("")
                self._btn_delete.setEnabled(False)
                return
            self._head_avatar.set_person(member.name, member.color_hint)
            self._head_title.setText(member.name)
            self._name_edit.setText(member.name)
            self._pinyin_edit.setText(member.pinyin)
            role_keys = list(ROLE_OPTIONS.keys())
            self._role_combo.setCurrentIndex(
                role_keys.index(member.role) if member.role in role_keys else 0)
            # purpose: "(无默认)" 的 data 是 ""
            target_purpose = member.default_purpose or ""
            for i in range(self._purpose_combo.count()):
                if self._purpose_combo.itemData(i) == target_purpose:
                    self._purpose_combo.setCurrentIndex(i)
                    break
            self._star_spin.setValue(member.starred)
            self._pin_btn.setChecked(member.pinned)
            self._note_edit.setPlainText(member.note)
            self._info_lbl.setText(
                f"创建于 {member.created_at or '(未知)'} · 最近使用 {_human_relative_time(member.last_used_at)}"
            )
            self._btn_delete.setEnabled(True)
        finally:
            self._block_edit_signals(False)

    def _block_edit_signals(self, on: bool) -> None:
        for w in (self._name_edit, self._pinyin_edit, self._note_edit,
                  self._role_combo, self._purpose_combo, self._star_spin, self._pin_btn):
            w.blockSignals(on)

    def _flush_panel_to_member(self) -> None:
        """把编辑面板的字段写回当前 TeamMember (不立刻 save 文件)。"""
        if self._current is None:
            return
        new_name = self._name_edit.text().strip()
        if not new_name:
            return  # 不允许空名
        # 重名校验 (若改名碰到别的成员)
        if new_name != self._current.name:
            for m in self._members:
                if m is not self._current and m.name == new_name:
                    QMessageBox.warning(self, "重名", f"姓名「{new_name}」已存在,请改其它。")
                    self._name_edit.setText(self._current.name)
                    return
        old_name = self._current.name
        self._current.name = new_name
        self._current.pinyin = self._pinyin_edit.text().strip()
        self._current.role = self._role_combo.currentData() or "recorder"
        self._current.default_purpose = self._purpose_combo.currentData() or ""
        self._current.starred = self._star_spin.value()
        self._current.pinned = self._pin_btn.isChecked()
        self._current.note = self._note_edit.toPlainText().strip()
        # P2 审查修复:name 变 → 头像色需重算(color_hint 是 name 的 MD5 hash)
        if new_name != old_name:
            self._current.color_hint = ""
        self._current.ensure_color()
        # 头像/标题立即刷新
        self._head_avatar.set_person(self._current.name, self._current.color_hint)
        self._head_title.setText(self._current.name)

    # ---- Slots ----
    def _on_search_changed(self, _text: str) -> None:
        self._refresh_list()

    def _on_select(self) -> None:
        # 切换前把面板回写
        if self._current is not None:
            self._flush_panel_to_member()
        items = self._list.selectedItems()
        if not items:
            self._render_panel(None)
            return
        name = items[0].data(Qt.UserRole)
        for m in self._members:
            if m.name == name:
                self._render_panel(m)
                return

    def _on_rename_inline(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.UserRole)
        for m in self._members:
            if m.name == name:
                new_name, ok = QInputDialog.getText(
                    self, "快速改名", "新姓名:", QLineEdit.Normal, m.name
                )
                if ok and new_name.strip() and new_name.strip() != m.name:
                    # 重名校验
                    if any(o.name == new_name.strip() for o in self._members if o is not m):
                        QMessageBox.warning(self, "重名", f"姓名「{new_name}」已存在。")
                        return
                    m.name = new_name.strip()
                    # P2 审查修复:rename 后头像色 hash 算自姓名,需重算
                    m.color_hint = ""
                    m.ensure_color()
                    self._dirty = True
                    self._refresh_list()
                return

    def _on_add(self) -> None:
        # 先回写当前面板
        if self._current is not None:
            self._flush_panel_to_member()
        name, ok = QInputDialog.getText(self, "新增人员", "姓名:")
        name = name.strip()
        if not ok or not name:
            return
        if any(m.name == name for m in self._members):
            QMessageBox.warning(self, "重名", f"「{name}」已在团队库内。")
            return
        new_m = TeamMember(
            name=name,
            created_at=datetime.now().isoformat(sep=" ", timespec="seconds"),
        )
        new_m.ensure_color()
        self._members.append(new_m)
        self._dirty = True
        self._refresh_list()
        # 自动选中新建项
        for i in range(self._list.count()):
            if self._list.item(i).data(Qt.UserRole) == name:
                self._list.setCurrentRow(i)
                break

    def _on_delete(self) -> None:
        if self._current is None:
            return
        ret = QMessageBox.question(
            self, "确认删除",
            f"删除人员「{self._current.name}」?\n"
            "(已写入 alloc_log / 标本表的历史记录不会被删除,仅团队库内移除。)",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        self._members = [m for m in self._members if m is not self._current]
        self._current = None
        self._dirty = True
        self._refresh_list()
        if self._members:
            self._list.setCurrentRow(0)
        else:
            self._render_panel(None)

    def _on_pin_toggled(self, _checked: bool) -> None:
        self._mark_dirty()

    def _mark_dirty(self, *args) -> None:
        self._dirty = True

    def _on_save_and_apply(self) -> None:
        if self._current is not None:
            self._flush_panel_to_member()
        # 排序
        self._members.sort(key=sort_key)
        save_members(self._members, self._workspace)
        self._dirty = False
        self._refresh_list()
        QMessageBox.information(self, "已保存", f"已保存 {len(self._members)} 名团队成员。")

    # ---- CSV ----
    def _on_import_csv(self) -> None:
        path_s, _ = QFileDialog.getOpenFileName(
            self, "导入 CSV (列:姓名,拼音,角色,星标,钉位,默认用途,备注)", "",
            "CSV 文件 (*.csv);;全部 (*.*)"
        )
        if not path_s:
            return
        try:
            added, skipped = self._do_import_csv(Path(path_s))
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        self._refresh_list()
        self._dirty = True
        QMessageBox.information(self, "导入完成", f"新增 {added} 人,跳过 {skipped} 重名。")

    def _do_import_csv(self, path: Path) -> tuple[int, int]:
        existing = {m.name for m in self._members}
        added = 0
        skipped = 0
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # 列名容错:姓名/Name/name
                name = (row.get("姓名") or row.get("Name") or row.get("name") or "").strip()
                if not name:
                    continue
                if name in existing:
                    skipped += 1
                    continue
                role_raw = (row.get("角色") or row.get("Role") or row.get("role") or "recorder").strip()
                # 中文角色名 → key
                role = role_raw
                for k, v in ROLE_OPTIONS.items():
                    if role_raw == v:
                        role = k
                        break
                if role not in ROLE_OPTIONS:
                    role = "recorder"
                try:
                    starred = max(0, min(5, int(row.get("星标") or row.get("Starred") or 0)))
                except Exception:
                    starred = 0
                pinned_raw = str(row.get("钉位") or row.get("Pinned") or "").strip().lower()
                pinned = pinned_raw in ("是", "true", "1", "yes", "y")
                m = TeamMember(
                    name=name,
                    pinyin=(row.get("拼音") or row.get("Pinyin") or "").strip(),
                    role=role,
                    starred=starred,
                    pinned=pinned,
                    default_purpose=(row.get("默认用途") or "").strip(),
                    note=(row.get("备注") or row.get("Note") or "").strip(),
                    created_at=datetime.now().isoformat(sep=" ", timespec="seconds"),
                )
                m.ensure_color()
                self._members.append(m)
                existing.add(name)
                added += 1
        return added, skipped

    def _on_export_csv(self) -> None:
        path_s, _ = QFileDialog.getSaveFileName(
            self, "导出团队成员为 CSV", "team_members.csv", "CSV 文件 (*.csv)"
        )
        if not path_s:
            return
        # P2 审查修复:CSV 公式注入防御 — 单元格首字 = / + / - / @ 在 Excel 打开会被当作公式,
        # 用户输入恶意 name/note 可触发 RCE。统一走 sanitize_csv_value。
        from .spreadsheet_preview import sanitize_csv_value as _sv
        try:
            with open(path_s, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["姓名", "拼音", "角色", "星标", "钉位",
                                 "默认用途", "备注", "创建时间", "最近使用"])
                for m in self._members:
                    writer.writerow([
                        _sv(m.name), _sv(m.pinyin), _sv(ROLE_OPTIONS.get(m.role, m.role)),
                        _sv(m.starred), _sv("是" if m.pinned else ""),
                        _sv(m.default_purpose), _sv(m.note), _sv(m.created_at), _sv(m.last_used_at),
                    ])
            QMessageBox.information(self, "已导出", f"导出 {len(self._members)} 人到\n{path_s}")
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    def reject(self) -> None:
        # 关闭时若 dirty,询问保存
        if self._dirty:
            ret = QMessageBox.question(
                self, "未保存", "有未保存的修改,是否保存?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if ret == QMessageBox.Cancel:
                return
            if ret == QMessageBox.Yes:
                self._on_save_and_apply()
        super().reject()
