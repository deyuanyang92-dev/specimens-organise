
# 2026-10-02：派生缓存（图片索引 / 缩略图）默认写本机 app 配置目录；测试全部重定向到临时目录，
# 不污染开发机，也不让用例之间互相看到对方的缓存。
import os as _os
import tempfile as _tempfile

# 2026-10-04：Windows CI 的临时目录是 8.3 短名（C:\Users\RUNNER~1\...），代码里 resolve() 后变长名
# （runneradmin），十几个用例因"同一路径两种写法"比较失败。测试统一用解析后的长路径。
from pathlib import Path as _Path  # noqa: E402

_tempfile.tempdir = str(_Path(_tempfile.gettempdir()).resolve())

_os.environ.setdefault("SPECIMEN_LOCAL_CACHE_DIR", _tempfile.mkdtemp(prefix="specimen_cache_test_"))

# 2026-10-04：配置目录（崩溃日志 / 恢复日志 / 本机备份 / 升级文件 / settings.json）也重定向到临时目录。
# app_config_dir() 优先读 APPDATA —— Windows CI 上若用真实 APPDATA，上一个用例留下的恢复日志、
# "上次未正常退出"标记等会让后续用例弹出模态框而挂住。
_os.environ["SPECIMEN_NO_SYSTEM_DIALOGS"] = "1"  # 子进程里的 Windows 系统错误框也不弹（boot_guard）
_os.environ["APPDATA"] = _tempfile.mkdtemp(prefix="specimen_appdata_test_")

# 文件被占用的重试在测试里调成几毫秒：故意模拟占用的用例不必每次真等 1.5 s。
from specimen_app import table_backend as _tb  # noqa: E402

_tb._LOCK_RETRY_DELAYS = (0.001, 0.001, 0.001, 0.001, 0.001)

# 2026-10-04：登记测试里打开的 ExcelStore，清理临时目录前统一 close()。
# Linux 允许删除仍打开的文件，Windows 不允许（WinError 32）——此前 test_core 在 Windows 上 57 个用例
# 因 操作记录.sqlite 连接未关而在 tearDown 报错；这类问题过去从未在 Windows 上跑过测试所以没发现。
import weakref as _weakref  # noqa: E402

from specimen_app import excel_store as _es  # noqa: E402

_OPEN_STORES: "_weakref.WeakSet" = _weakref.WeakSet()
_orig_store_init = _es.ExcelStore.__init__


def _tracking_store_init(self, *args, **kwargs):
    _orig_store_init(self, *args, **kwargs)
    _OPEN_STORES.add(self)


_es.ExcelStore.__init__ = _tracking_store_init


def close_all_open_stores(keep=None) -> None:
    import gc

    for store in list(_OPEN_STORES):
        if store is keep:
            continue
        try:
            store.close()
        except Exception:
            pass
    gc.collect()  # 已无人引用但未关闭的 sqlite 连接（如快照还原换下的旧连接）也一并释放


def closing_other_stores(aggregate):
    """包装合并函数：调用前关闭除目标库外所有测试里打开的 store。

    真实场景里 incoming/ 下的来源工作区来自别的电脑，本进程不会开着它们；测试造来源时开了没关，
    Windows 上目录改名加锁就失败（WinError 5/32），而 Linux 不受影响。
    """
    def _wrapped(target, *args, **kwargs):
        close_all_open_stores(keep=target)
        return aggregate(target, *args, **kwargs)
    return _wrapped
