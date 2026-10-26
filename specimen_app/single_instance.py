"""单实例守卫（v0.10.46）。

问题（boot_fault.log 实证）：启动要几秒，用户见窗口没出来就再点一次图标，于是同时
起两个进程 —— ``boot 2026-10-26T22:09:31 pid=10732`` / ``22:09:32 pid=6844``。
两个进程抢同一个工作区：一个拿到 ``数据/.workspace.lock``，另一个卡在重试/只读，
splash 的进度条一直转，内存和磁盘 IO 翻倍。

做法：进程级锁 + 本地 socket 通知。
- ``QLockFile`` 负责"谁是唯一实例"。它自带陈旧锁检测（记录 pid/主机名，进程没了
  就自动接管），所以上次崩溃留下的锁不会把软件永久锁死。
- ``QLocalServer`` / ``QLocalSocket`` 负责"把已有窗口叫到前台"：后启动的进程连上
  已有实例发一个字节就退出，已有实例收到后 raise + activate 自己的窗口。

只依赖 QtCore / QtNetwork，不碰业务模块，可以在启动链最早期调用。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable, Optional

from PyQt5.QtCore import QLockFile, QObject, pyqtSignal
from PyQt5.QtNetwork import QLocalServer, QLocalSocket


# 锁被认为陈旧的时长：进程仍在跑但长时间没刷新锁时才生效。QLockFile 另有 pid 存活
# 检测，正常崩溃留下的锁会被立刻识别，不必等满这个时间。
_STALE_LOCK_MS = 30_000
# 后启动进程连接已有实例的等待上限。连不上就当没有已有实例（它可能正在退出）。
_CONNECT_TIMEOUT_MS = 1_000


def _runtime_dir() -> Path:
    """放锁文件的目录。优先用户态可写目录，退回系统临时目录。"""
    base = os.environ.get("APPDATA")
    if base:
        d = Path(base) / "标本入库管理"
    else:
        d = Path.home() / ".specimen_inventory"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return Path(tempfile.gettempdir())


def _server_name(app_id: str) -> str:
    """本地 socket 名。带 uid/用户名，避免多用户互相抢同一个管道名。"""
    who = os.environ.get("USERNAME") or os.environ.get("USER") or str(os.getuid() if hasattr(os, "getuid") else 0)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in f"{app_id}-{who}")
    return safe[:80]  # Windows 命名管道名长度有限


class SingleInstanceGuard(QObject):
    """唯一实例持有者。构造成功即代表本进程是唯一实例。

    另一个进程尝试启动时发 ``activate_requested``，调用方据此把主窗口拉到前台。
    """

    activate_requested = pyqtSignal()

    def __init__(self, lock: Optional[QLockFile], server: Optional[QLocalServer], parent=None) -> None:
        super().__init__(parent)
        self._lock = lock
        self._server = server
        if server is not None:
            server.newConnection.connect(self._on_new_connection)

    def _on_new_connection(self) -> None:
        if self._server is None:
            return
        conn = self._server.nextPendingConnection()
        if conn is None:
            return
        # 内容无所谓，连上来本身就是"请把窗口拉到前台"的信号。
        conn.disconnected.connect(conn.deleteLater)
        conn.close()
        self.activate_requested.emit()

    def release(self) -> None:
        """退出时释放。重复调用安全。"""
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None
        if self._lock is not None:
            try:
                self._lock.unlock()
            except Exception:
                pass
            self._lock = None


def notify_existing_instance(app_id: str = "specimen-organise") -> bool:
    """告诉已在运行的实例"把窗口拉到前台"。返回是否送达。"""
    sock = QLocalSocket()
    sock.connectToServer(_server_name(app_id))
    if not sock.waitForConnected(_CONNECT_TIMEOUT_MS):
        return False
    try:
        sock.write(b"1")
        sock.flush()
        sock.waitForBytesWritten(_CONNECT_TIMEOUT_MS)
    finally:
        sock.disconnectFromServer()
    return True


def acquire(app_id: str = "specimen-organise") -> Optional[SingleInstanceGuard]:
    """尝试成为唯一实例。

    返回 guard = 本进程是唯一实例，调用方继续启动。
    返回 None = 已有实例在跑（已通知它激活窗口），调用方应立即退出。

    任何 Qt / 文件系统异常都按"拿到锁"处理 —— 单实例只是体验优化，不能因为它
    本身出问题就让用户打不开软件。
    """
    try:
        lock = QLockFile(str(_runtime_dir() / f"{app_id}.lock"))
        lock.setStaleLockTime(_STALE_LOCK_MS)
        if not lock.tryLock(100):
            # 已有实例（或锁陈旧但 QLockFile 判定仍有效）→ 叫它到前台，本进程退出。
            notify_existing_instance(app_id)
            return None
        name = _server_name(app_id)
        server = QLocalServer()
        # 上一次非正常退出可能留下 socket 文件（Unix）；listen 前先清掉。
        QLocalServer.removeServer(name)
        if not server.listen(name):
            server = None  # 监听失败只是收不到"激活"通知，不影响本进程正常跑
        return SingleInstanceGuard(lock, server)
    except Exception:
        # 兜底分支不能再碰 QLockFile —— 失败的往往正是它。返回一个空 guard：
        # 不做单实例限制，但 release() / activate_requested 的接口保持可用。
        return SingleInstanceGuard(None, None)
