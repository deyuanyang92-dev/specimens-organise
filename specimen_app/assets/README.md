# specimen_app/assets/

存放打包进 PyInstaller 分发包的二进制资源。

## worms_cache_bootstrap.sqlite.gz

**用途**：WoRMS 启动缓存。app 首次运行（用户缓存为空）时自动解压安装到
`~/.specimen_inventory/worms_cache.sqlite`（Linux）或
`%APPDATA%/标本入库管理/worms_cache.sqlite`（Windows），让离线环境也能
查 WoRMS 分类数据。

**生成方式**（在联网机器上执行一次）：

```bash
cd specimens-organise
python tools/build_worms_cache.py --download
# 输出: worms_cache_YYYY-QN.sqlite.gz（季度版本号）
mv worms_cache_*.sqlite.gz specimen_app/assets/worms_cache_bootstrap.sqlite.gz
```

季度更新一次即可（WoRMS 数据更新频率）。

**文件大小**：~15 MB（gzip 压缩），~50 MB（解压后 SQLite）。

**校验**：`install_cache_gz()` 解压后会 `SELECT COUNT(*) FROM worms_taxa`
做 SQLite 完整性检查。损坏文件不会覆盖现有缓存。

**注入逻辑**：见 `specimen_app/worms_client.py:ensure_bootstrap_cache()`。

**打包配置**：见 `build_release.py` 中 `--add-data` 配置（仅当文件存在时打包）。
