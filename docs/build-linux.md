# Linux 打包与运行教程

## 直接运行（源码模式）

### 1. 安装 Python 依赖

软件基于 PyQt5，无需额外安装系统级 GUI 包（PyQt5 通过 pip 安装，自带 Qt 运行时）。

```bash
pip install -r requirements.txt
```

`requirements.txt` 包含：PyQt5 / openpyxl / Pillow / tifffile。

### 2. 运行

```bash
python3 run_app.py
```

指定工作区：

```bash
python3 run_app.py --workspace "/path/to/workspace"
```

### 3. 中文字体

如果界面中文显示为方块，安装中文字体：

```bash
# Debian/Ubuntu
sudo apt install fonts-noto-cjk

# Fedora
sudo dnf install google-noto-sans-cjk-fonts

# Arch Linux
sudo pacman -S noto-fonts-cjk
```

---

## 构建可分发版本

### 1. 安装 PyInstaller

```bash
pip install pyinstaller
```

### 2. 构建

```bash
python3 build_release.py --version 0.4.0
```

构建产物在 `releases/v0.4.0/` 下：

```
releases/v0.4.0/
  ├── 标本入库管理_v0.4.0/         # 程序目录（onedir 模式）
  │   ├── 标本入库管理_v0.4.0     # 主程序（无后缀）
  │   └── _internal/              # Qt 运行时等
  ├── 标本入库管理_v0.4.0_linux.zip
  ├── update_manifest_linux.json
  └── sha256.txt
```

### 3. 分发

将 zip 发给用户，或直接分发目录：

```bash
# 用户端运行
unzip 标本入库管理_v0.4.0_linux.zip
cd 标本入库管理_v0.4.0/
chmod +x 标本入库管理_v0.4.0
./标本入库管理_v0.4.0
```

---

## 常见问题

### Wayland 显示异常

```bash
QT_QPA_PLATFORM=xcb python3 run_app.py
```

### 远程 SSH 使用（X11 转发）

```bash
ssh -X user@server
python3 run_app.py
```

### 多发行版兼容性

在较旧的系统上构建（如 Ubuntu 20.04），glibc 版本较低，可在更多发行版上运行：

```bash
# 使用 Docker 在旧版 Ubuntu 构建
docker run -it ubuntu:20.04 bash
# 容器内安装 Python + pip + requirements + pyinstaller，然后构建
```

### 缺少 xcb 插件

如果报 `qt.qpa.plugin: Could not load the Qt platform plugin "xcb"`：

```bash
# Debian/Ubuntu
sudo apt install libxcb-xinerama0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-xkb1 libxkbcommon-x11-0
```
