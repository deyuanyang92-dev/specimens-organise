# 发布与应用内更新

本文说明如何发布新版本，以及应用内"检查 GitHub 更新"的工作机制。

## 设计目标：可回退优先

定期发新版，但用户已录入的数据必须始终可用。因此更新机制刻意做得"保守"：

- **不覆盖旧版**：每个版本独立解压到 `releases/v{version}/`，新旧并存。
- **不自动启动**：应用内更新只负责*下载解压*，由用户在"版本管理 → 软件版本"里手动选择启动。
- **可回退**：切换版本前会提示创建数据快照（存于 `数据/数据版本/`）；新版本若与旧数据不兼容，可在"工作区数据版本"里回退。
- **降级写保护**：旧版本打开被新版本写过的工作区时，`excel_store` 会因 `data_schema_version` 过高而阻止写入，避免数据损坏。

## 发布流程

1. 更新 `specimen_app/__init__.py` 的 `__version__`。
2. 提交后打 tag 并推送：

   ```bash
   git tag v0.3.1
   git push origin v0.3.1
   ```

3. `.github/workflows/release.yml` 自动触发：在 `windows-latest` 和 `ubuntu-latest` 上各跑
   `python build_release.py --version 0.3.1`，把以下资产上传到对应的 GitHub Release：

   - `setup_v0.3.1_{平台}.zip` + `.sha256` —— 完整包（向后兼容 / 回退用；旧名含中文，GitHub Actions 上传后中文部分丢失，已改为 setup_ 前缀）
   - `app_v0.3.1_{平台}.zip` + `.sha256` —— **应用包**（小，应用代码，每版都变）
   - `runtime_{平台}_{hash}.zip` + `.sha256` —— **运行时包**（大，Python/PyQt5 等，按内容 hash 命名）
   - `update_manifest_{平台}.json` —— 增量更新清单，客户端据此决定怎么下

4. 在 GitHub Release 页面补充更新说明（release body）—— 应用内检查更新时会展示给用户。

> 也可以本地手动构建：`python build_release.py --version 0.3.1` 会在
> `releases/v0.3.1/` 下生成同样的全部资产，手动上传到 Release 即可。

### 资产命名约定

应用内更新器据此挑选下载包，**不要随意改名**：

- 完整 zip：`*_windows.zip` / `*_linux.zip` / `*_macos.zip`（文件名含平台关键字）。
- 应用包：`app_v{version}_{平台}.zip`；运行时包：`runtime_{平台}_{runtime_hash}.zip`。
- 校验文件：`{zip 文件名}.sha256`，内容为 `{sha256}  {zip 文件名}`。
- 增量清单：`update_manifest_{平台}.json`（按平台命名，避免多平台 CI 互相覆盖）。

## 增量更新：拆分运行时包 + 应用包

PyInstaller onedir 包里 95%+ 是第三方运行时（`_internal/` 的 Python、PyQt5…），版本间几乎不变；
真正每版都变的只有应用代码（`_internal/specimen_app/` 的 .pyc，几百 KB）。所以发版时拆成两个包：

- **应用分区** = 根目录 exe ∪ `_internal/specimen_app/**` ∪ `.update_meta.json` → 打进 `app_*.zip`
- **运行时分区** = 其余全部 → 打进 `runtime_*_{hash}.zip`，`hash` 是运行时分区文件内容的 sha256 前 12 位

每个安装好的 `releases/v{X}/标本入库管理_v{X}/` 里都有一个 `.update_meta.json`，记录其
`runtime_hash` 和 `app_files` —— 让它日后能作为"运行时复用源"。

## 应用内更新机制

入口：**版本管理 → 软件版本 → 「检查 GitHub 更新」**。

1. 后台线程请求 `https://api.github.com/repos/deyuanyang92-dev/specimens-organise/releases/latest`
   （公开仓库免鉴权），按平台找完整 zip 与 `update_manifest_{平台}.json`。
2. 比较版本号（`updater.is_newer`，预发布后缀如 `-test.1` 排序低于正式版）。
3. 有新版 → 弹窗显示更新说明 → 用户确认 → `updater.download_update()`：
   - **有 `update_manifest`**：扫描本地 `releases/` 找 `runtime_hash` 匹配的已装版本。
     - **匹配到**（增量路径）：只下载 `app_*.zip`，运行时文件从本地旧版本拷贝复用；
     - **没匹配**（首装 / 依赖升级）：下载 `app_*.zip` + `runtime_*.zip`。
   - **无 `update_manifest`**（老 release）：回退到完整 zip 下载。
   - 下载物**强制 sha256 校验**，解压带 zip-slip 防护，全程在临时目录完成后再移入
     `releases/v{version}/` —— 仍是完整独立目录，旧版本全保留。
4. 用户在列表里选中新版本 → 「启动选中版本」→ 提示创建数据快照 → 启动。

安全约束：只允许 HTTPS、只允许 `github.com` / `githubusercontent.com` 域名、sha256 不匹配即中止、
解压与复用路径都做逃逸校验。复用的运行时文件来自本地之前已校验安装的目录，视为可信。

### 启动时自动检查（可选）

设置对话框里勾选「启动时自动检查 GitHub 更新」后，应用启动 ~2.5 秒后会后台检查一次
（距上次检查不足 24 小时则跳过）。有新版只在状态栏静默提示，不弹窗打扰；失败完全静默。

## 实现位置

- `specimen_app/updater.py` —— 纯标准库的检查/下载/校验/解压逻辑：`check_latest_release`、
  `download_release`（完整 zip / 回退）、`download_update`（增量入口）、`partition` 复用源扫描。
- `specimen_app/ui.py` —— `UpdateCheckWorker` / `UpdateDownloadWorker` 后台线程；
  `VersionManagerDialog` 的更新按钮；`SpecimenWindow._maybe_check_updates_on_startup` 启动检查。
- `specimen_app/app_settings.py` —— `check_updates_on_startup` / `last_update_check` 设置项。
- `build_release.py` —— `partition_bundle()` 拆分；构建后打包完整 zip + 应用包 + 运行时包 +
  各 `.sha256` + `update_manifest_{平台}.json`，并在 bundle 内写 `.update_meta.json`。
- `.github/workflows/release.yml` —— tag 触发的跨平台构建与资产上传。
