# Windows 打包说明

有三种方式拿到 Windows exe。**平时发版用方式一，什么都不用在本机装。**

## 方式一：自动打包并发布（推荐）

GitHub 在自己的 Windows 机器上打包、试跑、上传 Release，用户在软件里「检查更新」就能收到。

1. 改版本号：`specimen_app/__init__.py` 里的 `__version__`（例如 `0.10.40`）。
2. 提交并推送到 `main`。
3. 打标签并推送：
   ```bash
   git tag v0.10.40
   git push origin v0.10.40
   ```
4. 约 3 分钟后，在 GitHub → Releases 出现 `v0.10.40`，包含：
   - `installer_v0.10.40_windows.exe`：安装器（推荐给用户）
   - `setup_v0.10.40_windows.zip`：便携版（整体解压运行）
   - `app_v*.zip` / `update_manifest_*.json`：给软件内自动更新用，不要手动下载

流程定义在 `.github/workflows/release.yml`：先在 Windows + Linux 上跑全量测试（`ci.yml`），**测试不过不打包**；
打包后再真实启动 exe 跑 `--smoke`，启动失败也**不发布**。每次普通推送也会自动跑同一套测试。
标签名必须和 `__version__` 一致，否则自动更新会比对错版本。

## 方式二：GitHub 上手动点按钮打包（不发布）

适合发版前先试装一下，或者临时给某个用户一个包。

1. 打开 GitHub 仓库 → **Actions** → 左侧选 **Release** → 右上 **Run workflow** → 选分支 → **Run workflow**。
2. 跑完后进入这次运行的页面，底部 **Artifacts** 里下载
   `specimen-organise-v<版本>-windows`（内含安装器和便携版 zip）。
3. 这种方式**不会**创建 Release，也不会推送给用户的自动更新；Artifacts 保留 14 天。

## 方式三：在自己的 Windows 电脑上打包

### 准备（只做一次）

1. 安装 Python 3.11（64 位）：<https://www.python.org/downloads/>，安装时勾选 **Add Python to PATH**。
2. （可选）安装 Inno Setup 6：<https://jrsoftware.org/isdl.php>。装了才会生成安装器 `installer_*.exe`；
   不装也能出便携版 zip。
3. 拿到源码：`git clone` 仓库，或在 GitHub 上 Code → Download ZIP 后解压。

### 一键打包

双击项目根目录的 **`build.bat`**。它会依次：

1. 检查 Python；
2. 安装依赖（`requirements.txt` + PyInstaller）；
3. 运行 `python build_release.py`；
4. 试跑打包好的 exe（`--smoke`），失败会提示日志位置；
5. 打开输出目录 `releases\v<版本>\`。

### 命令行打包（等价）

```bat
pip install -r requirements.txt pyinstaller
set PYTHONUTF8=1
python build_release.py --version 0.10.40
```

产物在 `releases\v0.10.40\`。

### 常见问题

| 现象 | 处理 |
|---|---|
| `UnicodeEncodeError` | 先执行 `set PYTHONUTF8=1`（`build.bat` 已自动处理路径输出） |
| 没生成 `installer_*.exe` | 没装 Inno Setup 6，或不在默认路径；可设环境变量 `INNO_SETUP_COMPILER=...\ISCC.exe` |
| 只拷贝 exe 到别处运行报 `Failed to load Python DLL` | 便携版必须整个文件夹一起解压使用 |
| 试跑失败 | 看 `%APPDATA%\标本入库管理\startup_failure_*.log` |
