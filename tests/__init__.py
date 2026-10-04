
# 2026-10-02：派生缓存（图片索引 / 缩略图）默认写本机 app 配置目录；测试全部重定向到临时目录，
# 不污染开发机，也不让用例之间互相看到对方的缓存。
import os as _os
import tempfile as _tempfile

_os.environ.setdefault("SPECIMEN_LOCAL_CACHE_DIR", _tempfile.mkdtemp(prefix="specimen_cache_test_"))

# 2026-10-04：配置目录（崩溃日志 / 恢复日志 / 本机备份 / 升级文件 / settings.json）也重定向到临时目录。
# app_config_dir() 优先读 APPDATA —— Windows CI 上若用真实 APPDATA，上一个用例留下的恢复日志、
# "上次未正常退出"标记等会让后续用例弹出模态框而挂住。
_os.environ["APPDATA"] = _tempfile.mkdtemp(prefix="specimen_appdata_test_")

# 文件被占用的重试在测试里调成几毫秒：故意模拟占用的用例不必每次真等 1.5 s。
from specimen_app import table_backend as _tb  # noqa: E402

_tb._LOCK_RETRY_DELAYS = (0.001, 0.001, 0.001, 0.001, 0.001)
