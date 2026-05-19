"""手动添加入库编号对话框 (规范化软件设计 2026-05 Phase 5)。

功能:
- 多行粘贴 / 单条输入
- 解析行 → voucher 列表 + 校验 (重复 / 已存在 / 空)
- ≥2 个有效 voucher → 自动调 infer_pattern → 显推断规则
- "按规则批量生成 N 个" 按钮 (用户输 N)
- "添加" 提交 → 调 store.create_specimen_with_voucher
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
)

from .voucher_pattern import infer_pattern, generate_batch


class ManualVoucherDialog(QDialog):
    def __init__(self, store, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("手动添加入库编号")
        self.resize(560, 480)
        self._store = store
        self._parent_window = parent
        self._pattern: Optional[dict] = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            "<b>每行一个入库编号</b>。可粘贴多行;或手输 2 个以上,系统自动识别规则后可批量生成。\n"
            "示例:YZZ005001 / QD-LSD-001 / 自定义任意字串。"
        ))

        self._text = QTextEdit()
        self._text.setPlaceholderText("一行一个,如\nYZZ005001\nYZZ005002\nYZZ005003")
        self._text.textChanged.connect(self._on_text_changed)
        layout.addWidget(self._text, 1)

        # 规则推断显示行
        pat_row = QHBoxLayout()
        self._pattern_label = QLabel("尚未识别规则(输入 ≥2 个 voucher 自动推断)")
        self._pattern_label.setStyleSheet("color: #888;")
        pat_row.addWidget(self._pattern_label, 1)
        self._batch_spin = QSpinBox()
        self._batch_spin.setRange(1, 9999)
        self._batch_spin.setValue(10)
        self._batch_spin.setSuffix(" 个")
        self._batch_spin.setEnabled(False)
        pat_row.addWidget(self._batch_spin)
        self._batch_btn = QPushButton("按规则追加生成")
        self._batch_btn.setEnabled(False)
        self._batch_btn.clicked.connect(self._on_batch_generate)
        pat_row.addWidget(self._batch_btn)
        layout.addLayout(pat_row)

        # 校验状态行
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #555;")
        layout.addWidget(self._status_label)

        # 底部按钮
        btn_row = QHBoxLayout()
        self._save_series_btn = QPushButton("保存推断的规则为新系列…")
        self._save_series_btn.setEnabled(False)
        self._save_series_btn.clicked.connect(self._on_save_as_series)
        btn_row.addWidget(self._save_series_btn)
        btn_row.addStretch()
        self._ok_btn = QPushButton("添加全部")
        self._ok_btn.clicked.connect(self._on_submit)
        self._ok_btn.setDefault(True)
        btn_row.addWidget(self._ok_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    # ---- 数据 ----
    def _collect_vouchers(self) -> list[str]:
        raw = self._text.toPlainText() or ""
        out = []
        seen = set()
        for line in raw.splitlines():
            v = line.strip()
            if not v or v in seen:
                continue
            out.append(v)
            seen.add(v)
        return out

    def _check_existing(self, vouchers: list[str]) -> tuple[list[str], list[str]]:
        """返 (new, existing) — 已在数据库的 vs 新的。"""
        try:
            current = set(self._store.list_vouchers())
        except Exception:
            current = set()
        new = []
        existing = []
        for v in vouchers:
            if v in current:
                existing.append(v)
            else:
                new.append(v)
        return new, existing

    def _on_text_changed(self) -> None:
        vouchers = self._collect_vouchers()
        # 推断规则
        self._pattern = infer_pattern(vouchers)
        if self._pattern:
            ex = self._pattern["examples"]
            self._pattern_label.setText(
                f"<b>识别规则:</b> 前缀 <code>{self._pattern['prefix']}</code>, "
                f"位宽 {self._pattern['width']}, 步长 {self._pattern['step']}, "
                f"下一个 <code>{ex[-1]}</code>"
            )
            self._pattern_label.setStyleSheet("color: #1a5faa;")
            self._batch_spin.setEnabled(True)
            self._batch_btn.setEnabled(True)
            self._save_series_btn.setEnabled(True)
        else:
            self._pattern_label.setText("尚未识别规则(输入 ≥2 个 voucher 自动推断)")
            self._pattern_label.setStyleSheet("color: #888;")
            self._batch_spin.setEnabled(False)
            self._batch_btn.setEnabled(False)
            self._save_series_btn.setEnabled(False)
        # 校验状态
        new, existing = self._check_existing(vouchers)
        if not vouchers:
            self._status_label.setText("")
        elif existing:
            self._status_label.setText(
                f"⚠ {len(existing)} 个已存在(会跳过): {', '.join(existing[:5])}"
                + ("…" if len(existing) > 5 else "")
            )
            self._status_label.setStyleSheet("color: #c14d4d;")
        else:
            self._status_label.setText(f"✓ {len(new)} 个待添加,无冲突")
            self._status_label.setStyleSheet("color: #2a8c3a;")

    def _on_batch_generate(self) -> None:
        if not self._pattern:
            return
        n = self._batch_spin.value()
        new_vouchers = generate_batch(self._pattern, n)
        # 追加到现有文本
        cur = self._text.toPlainText().rstrip("\n")
        added = "\n".join(new_vouchers)
        if cur:
            self._text.setPlainText(cur + "\n" + added)
        else:
            self._text.setPlainText(added)

    def _on_save_as_series(self) -> None:
        if not self._pattern:
            return
        name, ok = QInputDialog.getText(self, "保存为新系列", "系列名称(如 'YZZ_新批次'):")
        name = (name or "").strip()
        if not ok or not name:
            return
        # 用 ensure_assignee_series 或类似 API,简化:直接写 config series
        try:
            cfg_series = self._store.config.setdefault("series", {})
            if name in cfg_series:
                QMessageBox.warning(self, "重名", f"系列「{name}」已存在")
                return
            cfg_series[name] = {
                "prefix": self._pattern["prefix"],
                "width": self._pattern["width"],
                "next_counter": self._pattern["next"],
            }
            self._store._save_config()
            QMessageBox.information(
                self, "已保存",
                f"系列「{name}」已保存。下次「批量生成编号」可选用,自动续号。"
            )
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))

    def _on_submit(self) -> None:
        vouchers = self._collect_vouchers()
        if not vouchers:
            QMessageBox.warning(self, "无内容", "请输入至少一个入库编号。")
            return
        new, existing = self._check_existing(vouchers)
        if not new:
            QMessageBox.warning(self, "全部已存在", "所有编号都已在数据库,无新增。")
            return
        if existing:
            ret = QMessageBox.question(
                self, "部分已存在",
                f"{len(existing)} 个编号已存在(会跳过)。\n"
                f"将新增 {len(new)} 个。继续?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if ret != QMessageBox.Yes:
                return
        # 批量创建
        ok_count = 0
        fail = []
        for v in new:
            try:
                self._store.create_specimen_with_voucher(v)
                ok_count += 1
            except Exception as exc:
                fail.append(f"{v}: {exc}")
        msg = f"成功创建 {ok_count} 个入库编号。"
        if fail:
            msg += f"\n失败 {len(fail)}:\n" + "\n".join(fail[:5])
            if len(fail) > 5:
                msg += "\n…"
        QMessageBox.information(self, "完成", msg)
        # 刷新主窗口
        if self._parent_window is not None and hasattr(self._parent_window, "refresh_list"):
            try:
                self._parent_window.refresh_list()
            except Exception:
                pass
        self.accept()
