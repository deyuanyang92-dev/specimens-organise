"""plan E1：照片归档目录管理 — 文件系统侧（不动 ``照片信息.xlsx``）。

设计原则
---------
``PhotoArchive`` 只关心 ``工作区/照片/`` 目录下的物理文件：
  - 工作区相对路径计算
  - 文件名规范化
  - 归档路径冲突解决（同名碰撞 → 加数字后缀）
  - 物理文件拷贝 / rename / unlink

``照片信息.xlsx`` 的行增删改全部留在 ``ExcelStore`` ——保持职责单一，
让本模块可在测试中纯 mock 文件系统而无 openpyxl 依赖。

依赖反转
---------
有的 PhotoArchive 方法需要查"某文件还有没有别的 photo row 在引用"——这是 xlsx 数据。
通过 ``read_photo_rows_callback`` 回调注入，避免反向 import ExcelStore（v0.10.0 仅暴露
接口，复杂方法的迁移留 v0.11.0）。

v0.10.0 范围（最小可工作版）
-----------------------------
本版本只把这 4 个**纯文件系统**辅助方法迁过来：
  - ``compute_workspace_archive_directory()``
  - ``compute_archive_relative_path()``
  - ``is_path_under_workspace_archive_directory()``
  - ``sanitize_photo_filename_for_storage()``

``ExcelStore`` 中原同名 ``_photo_archive_dir`` / ``_archive_relative_path`` /
``_is_workspace_archive_path`` / ``_safe_photo_filename`` 改为薄薄一层 delegator
（保持 backward-compatible，所有调用方代码不变）。

更复杂的方法（``_archive_photo_file`` / ``_delete_unreferenced_photo_file`` /
``_move_archive_file_to_name``）继续留在 ``ExcelStore``，v0.11.0 与
``_record_action`` 集中重构一起迁移。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

# 类型别名：read_rows("photo") 的返回；用 dict 而非自定义 Row 保持模块独立
PhotoRow = dict


class PhotoArchive:
    """plan E1：工作区 ``照片/`` 目录的文件系统侧管理类。

    回调依赖：``read_photo_rows_callback`` 应返回当前工作区全部 photo 行的列表
    （即 ``store.read_rows("photo")``）。本类不会主动调用——预留给 v0.11.0
    把 ``_delete_unreferenced_photo_file`` 等需要 photo 行引用计数的方法迁入。
    """

    _WORKSPACE_ARCHIVE_DIRECTORY_NAME = "照片"

    def __init__(
        self,
        workspace_root: Path,
        read_only: bool,
        read_photo_rows_callback: Callable[[], list[PhotoRow]],
    ) -> None:
        self._workspace_root = Path(workspace_root).resolve()
        self._read_only = bool(read_only)
        self._read_photo_rows_callback = read_photo_rows_callback

    def compute_workspace_archive_directory(self) -> Path:
        """返回工作区的归档目录绝对路径 ``<workspace_root>/照片``。"""
        return self._workspace_root / self._WORKSPACE_ARCHIVE_DIRECTORY_NAME

    def compute_archive_relative_path(self, absolute_path: Path) -> str:
        """计算 ``absolute_path`` 相对工作区根的路径字符串，前缀 ``./``。

        约定：``./照片/<filename>``。``照片信息.xlsx`` 的 ``相对路径`` 列存的就是这种格式。
        ``absolute_path`` 必须在工作区根之下，否则抛 ``ValueError``。
        """
        absolute_path = absolute_path.resolve()
        relative_to_root = absolute_path.relative_to(self._workspace_root)
        return "./" + relative_to_root.as_posix()

    def is_path_under_workspace_archive_directory(self, path: Path) -> bool:
        """判断 ``path`` 是否在 ``<workspace_root>/照片`` 目录下（不要求文件存在）。"""
        try:
            path.resolve().relative_to(self.compute_workspace_archive_directory().resolve())
            return True
        except ValueError:
            return False

    def sanitize_photo_filename_for_storage(
        self,
        filename: str,
        default_suffix: str = "",
    ) -> str:
        """清洗用户给的照片文件名，让它在工作区里安全存储。

        - 去掉目录部分，只留 basename
        - 替换非法字符（Windows 限制集）为下划线
        - 主文件名超 140 字符截断
        - 缺扩展名时补 ``default_suffix``
        - 名字完全空时回退到 "photo"
        """
        cleaned_basename = Path(filename or "photo").name.strip() or "photo"
        cleaned_basename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", cleaned_basename)
        path = Path(cleaned_basename)
        suffix = path.suffix or default_suffix
        stem = path.stem or "photo"
        if len(stem) > 140:
            stem = stem[:140].rstrip(" ._") or "photo"
        return f"{stem}{suffix}"
