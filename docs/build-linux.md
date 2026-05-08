# Linux 打包与运行教程

## 直接运行（开发者模式）

### 1. 安装系统依赖

Tkinter 通常需要单独安装系统包：

```bash
# Debian/Ubuntu
sudo apt install python3-tk

# Fedora/RHEL
sudo dnf install python3-tkinter

# Arch Linux
sudo pacman -S tk

# openSUSE
sudo zypper install python3-tk
```

### 2. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 3. 运行

```bash
python3 run_app.py
```

如果需要指定工作区：

```bash
python3 run_app.py --workspace "/path/to/workspace"
```

### 4. 可选：安装拖放支持

拖放功能需要 `tkinterdnd2`，在大多数 Linux 发行版上可以正常安装：

```bash
pip install tkinterdnd2
```

如果安装失败（如缺少 Tk 开发头文件），软件仍可通过"添加照片"按钮关联图片。

## 构建可分发版本

### 1. 安装 PyInstaller

```bash
pip install pyinstaller
```

### 2. 构建

```bash
python3 build_release.py --version 0.2.3
```

构建产物在 `releases/v0.2.3/` 下，是一个可执行文件（无 `.exe` 后缀）。

### 3. 分发

将整个输出目录打包分发：

```bash
cd releases/v0.2.3/
tar czf 标本入库管理_v0.2.3_linux.tar.gz 标本入库管理_v0.2.3/
```

用户解压后运行：

```bash
tar xzf 标本入库管理_v0.2.3_linux.tar.gz
cd 标本入库管理_v0.2.3/
chmod +x 标本入库管理_v0.2.3
./标本入库管理_v0.2.3
```

## 常见问题

### 1. tkinterdnd2 安装失败

`tkinterdnd2` 在 Linux 上需要 Tk 开发库。安装方式：

```bash
# Debian/Ubuntu
sudo apt install tk-dev

# Fedora
sudo dnf install tk-devel
```

如果仍然无法安装，可以跳过。软件会自动降级，使用"添加照片"按钮替代拖放。

### 2. 显示相关错误

如果遇到 `_tkinter.TclError: no display name` 或类似错误：
- 确保有图形桌面环境（X11 或 Wayland）
- 远程运行时需要 X11 转发：`ssh -X`
- 在 Wayland 下某些功能可能需要设置 `GDK_BACKEND=x11`

### 3. 字体问题

中文显示需要安装中文字体：

```bash
# Debian/Ubuntu
sudo apt install fonts-noto-cjk

# Fedora
sudo dnf install google-noto-sans-cjk-fonts
```

### 4. 打包后找不到 libtk

PyInstaller 可能遗漏 Tk 共享库。如果运行构建版本时报错，在 `build_release.py` 中添加：

```
--collect-all tkinter
```

### 5. 多发行版兼容性

为提高兼容性：
- 在较旧的 Linux 发行版上构建（如 Ubuntu 20.04），glibc 版本较低
- 使用 `manylinux` Docker 镜像作为构建环境
- 避免链接发行版特有的库版本
