
# 2026-10-02：派生缓存（图片索引 / 缩略图）默认写本机 app 配置目录；测试全部重定向到临时目录，
# 不污染开发机，也不让用例之间互相看到对方的缓存。
import os as _os
import tempfile as _tempfile

_os.environ.setdefault("SPECIMEN_LOCAL_CACHE_DIR", _tempfile.mkdtemp(prefix="specimen_cache_test_"))
