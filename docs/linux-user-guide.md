# 标本入库管理 — Linux 使用指南

## 一、环境安装

### 1.1 系统要求

- Linux 桌面环境（X11 或 Wayland）
- Python 3.9 或更高版本
- 至少 2GB 可用磁盘空间

### 1.2 安装 Tkinter（图形界面库）

Tkinter 是 Python 的标准 GUI 库，但大多数 Linux 发行版需要单独安装：

```bash
# Ubuntu / Debian / Linux Mint
sudo apt update
sudo apt install python3-tk

# Fedora / RHEL / CentOS
sudo dnf install python3-tkinter

# Arch Linux / Manjaro
sudo pacman -S tk

# openSUSE
sudo zypper install python3-tk

# Alpine Linux
sudo apk add py3-tkinter
```

验证安装：

```bash
python3 -c "import tkinter; print('Tkinter OK')"
```

### 1.3 安装中文字体

软件界面和字段名均为中文，需要确保系统有中文字体：

```bash
# Ubuntu / Debian
sudo apt install fonts-noto-cjk

# Fedora
sudo dnf install google-noto-sans-cjk-fonts

# Arch Linux
sudo pacman -S noto-fonts-cjk
```

安装后无需重启，新打开的程序即可使用。

### 1.4 获取项目代码

将项目目录复制到 Linux 机器上（U盘、网络共享、git clone 均可）。假设项目位于：

```
/home/用户名/标本整理/
```

### 1.5 安装 Python 依赖

在项目根目录下执行：

```bash
cd /home/用户名/标本整理
pip3 install -r requirements.txt
```

这会安装以下库：

| 库 | 用途 |
|---|------|
| `openpyxl` | Excel 文件读写 |
| `Pillow` | 图片处理（JPG/PNG/TIFF 预览） |
| `tifffile` | 大尺寸 TIFF 图片支持 |
| `tkinterdnd2` | 照片拖放功能（可选） |

### 1.6 安装拖放支持（可选）

如果 `pip install tkinterdnd2` 失败，需要先安装 Tk 开发库：

```bash
# Ubuntu / Debian
sudo apt install tk-dev

# Fedora
sudo dnf install tk-devel

# Arch Linux
sudo pacman -S tk
```

然后重新安装：

```bash
pip3 install tkinterdnd2
```

**未安装 tkinterdnd2 时**，软件正常运行，只是不能拖拽照片到预览区。可以通过"添加照片"按钮手动选择文件。

### 1.7 验证环境

```bash
cd /home/用户名/标本整理
python3 run_app.py
```

如果看到软件主窗口打开，说明环境准备完成。

---

## 二、启动与工作区

### 2.1 启动

```bash
cd /home/用户名/标本整理
python3 run_app.py
```

或者创建桌面快捷方式：

```bash
# 创建启动脚本
cat > ~/桌面/标本入库管理.sh << 'EOF'
#!/bin/bash
cd "/home/用户名/标本整理"
python3 run_app.py &
EOF
chmod +x ~/桌面/标本入库管理.sh
```

### 2.2 工作区概念

**工作区**是存放所有标本数据的目录。每个工作区包含：

```
我的工作区/
├── 数据/                    # 所有标本数据（自动创建）
│   ├── 标本信息.xlsx
│   ├── 照片信息.xlsx
│   ├── 分类信息.xlsx
│   ├── 编号索引.xlsx
│   ├── 修改记录.xlsx
│   ├── 操作记录.xlsx
│   ├── 工作区配置.json
│   ├── 缩略图缓存/          # 自动生成的缩略图
│   └── 数据版本/            # 快照备份数据
└── 字段模版/                 # 物种预设字段（可选）
    └── 表格信息预设字段.xlsx
```

**关键点：**
- 照片不会被复制到工作区，只记录相对路径
- 工作区可以整体移动/备份，数据不丢失
- 不要使用项目自身的 `build/`、`dist/`、`releases/` 目录作为工作区

### 2.3 指定工作区

```bash
# 命令行指定工作区
python3 run_app.py --workspace "/path/to/my-workspace"
```

启动后也可以在软件内切换工作区（菜单 → 切换工作区）。

### 2.4 自动记忆

软件会记住上次使用的工作区。下次启动时自动打开。

### 2.5 初始化新工作区

选择一个空目录作为工作区时，软件会弹窗询问是否初始化。确认后会自动创建 `数据/` 目录和所需文件。

---

## 三、主要功能使用

### 3.1 新建标本

1. 点击左上方 **"新建"** 按钮
2. 系统自动生成入库编号（格式：`YZZ000001`，自动递增）
3. 左侧列表中出现新条目

### 3.2 填写标本信息

选中左侧列表中的标本后，右侧出现三个信息面板：

**标本信息**（必填项带 *）：

| 字段 | 说明 |
|------|------|
| 入库编号* | 自动生成，格式 YZZ000001 |
| 管内编号* | 手动填写，格式如 `QD-CK-SC008-260827` |
| 保存方式 | 如酒精、干制等 |
| 采集日期 | 管内编号填好后自动解析 |
| 采集地点缩写* | 管内编号填好后自动解析 |
| 入库日期 | 手动填写 |
| 标本存放位置 | 手动填写 |
| 备注 | 自由填写 |

**管内编号自动解析示例**：
- 输入 `QD-CK-SC008-260827`
- 自动解析采集地点缩写：`QD-CK-SC008`
- 自动解析采集日期：`2026-08-27`

所有字段修改后 **自动保存**（500ms 防抖），无需手动保存。

### 3.3 填写分类信息

分类信息面板包含种名和科信息：

| 字段 | 说明 |
|------|------|
| 种名* | 输入种名片段后弹出候选列表，点击选择 |
| 种拉丁 | 对应拉丁名 |
| 科* | 同样支持搜索候选 |
| 科拉丁 | 对应拉丁名 |

**物种自动补全**：输入几个字后，系统自动从 `字段模版/表格信息预设字段.xlsx` 匹配候选物种。

### 3.4 管理照片

#### 添加照片

**方式一：拖拽**（需要 tkinterdnd2）
- 直接将图片文件从文件管理器拖到照片预览区

**方式二：按钮**
- 点击照片预览区上方的 **"添加照片"** 按钮
- 在文件选择对话框中选择一张或多张图片
- 支持 TIFF / JPG / PNG / BMP 格式

**方式三：图片检索**
- 点击 **"检索图片"** 按钮
- 弹出检索窗口，扫描工作区内所有图片
- 默认使用当前标本的管内编号作为搜索关键词
- 支持 Ctrl / Shift 多选
- 点击 **"关联选中"** 将图片批量关联到当前标本

#### 照片预览

- **单张模式**：显示当前选中照片
- **宫格模式**：右侧下拉框可选择 2/4/6/8 宫格
- 宫格中点击某张照片可放大为单张
- 单张模式下点击 **"返回宫格"** 恢复宫格

#### 照片交互

| 操作 | 方式 |
|------|------|
| 缩放 | `Ctrl + 鼠标滚轮` |
| 切换照片 | 鼠标滚轮（无 Ctrl） |
| 拖拽平移 | 鼠标左键拖动 |
| 适配窗口 | 双击照片 |
| 打开原图 | 右键菜单 → 打开原图 |
| 删除照片 | 右键菜单 → 删除照片 |
| 复制路径 | 右键菜单 → 复制相对路径 |

#### 照片信息表

右侧"照片信息"表格显示当前标本的所有照片记录，包含序号、文件名、相对路径和描述。点击任意行切换预览。

### 3.5 大图片支持

软件针对大尺寸 TIFF 图片（500MB 甚至 1GB+）做了专门优化：
- 缩略图使用 stride 下采样，不会占用过多内存
- 后台异步生成缩略图，不阻塞界面操作
- 宫格模式同时加载多张照片时自动使用缩略图

### 3.6 标本列表状态

左侧列表每个标本后显示三个状态标志：

```
YZZ000001  ●●●    表示：标本信息完整、有照片、分类信息完整
YZZ000002  ●●○    表示：标本信息完整、有照片、分类信息缺失
YZZ000003  ●○○    表示：标本信息完整、无照片、无分类
```

- ● = 完整
- ○ = 缺失

### 3.7 撤回与重做

- **撤回**：点击工具栏 **"撤回"** 按钮
- **重做**：点击工具栏 **"重做"** 按钮
- 支持撤回字段修改、照片添加/删除、新建标本、删除标本等操作
- 默认保留 200 步历史记录，可在设置中调整

### 3.8 删除标本

1. 在左侧列表选中标本
2. 点击 **"删除"** 按钮
3. 确认删除
4. 可通过撤回恢复

### 3.9 导入旧工作区

1. 菜单 → **导入工作区**
2. 选择旧工作区目录
3. 软件自动检查编号冲突
4. 如果有冲突，阻止导入并生成 `导入冲突报告_*.xlsx`
5. 无冲突时完成导入

### 3.10 切换工作区

1. 菜单 → **切换工作区**
2. 选择新的工作区目录
3. 软件保存当前状态并切换

### 3.11 版本管理

菜单 → **版本管理** 打开版本管理窗口：

**数据快照**：
- **创建快照**：保存当前工作区数据的完整副本
- **恢复快照**：将数据回退到某个快照的状态
- **打开目录**：查看快照文件

**软件版本**：
- 列出 `releases/` 下的所有历史版本
- 可以直接启动选中版本（用于测试不同版本）

### 3.12 设置

菜单 → **设置**：
- 调整撤回/重做保留步数
- 其他应用配置

---

## 四、数据文件说明

所有数据保存在工作区的 `数据/` 目录下：

| 文件 | 内容 |
|------|------|
| `标本信息.xlsx` | 标本基本字段（入库编号、管内编号、保存方式等） |
| `照片信息.xlsx` | 照片关联记录（入库编号、文件名、相对路径、描述） |
| `分类信息.xlsx` | 分类字段（入库编号、种名、科等） |
| `编号索引.xlsx` | 编号索引和指纹 |
| `修改记录.xlsx` | 字段修改明细和汇总 |
| `操作记录.xlsx` | 所有操作的完整日志（支持撤回/重做） |
| `数据版本记录.xlsx` | 快照版本历史 |
| `工作区配置.json` | 工作区元信息 |
| `缩略图缓存/` | 自动生成的 JPEG 缩略图 |
| `数据版本/` | 快照备份数据 |

**重要**：
- 照片不会被复制或移动，只记录相对路径
- 如果照片文件被移动或删除，相对路径失效，需要重新关联
- 可以直接备份 `数据/` 目录来保存所有数据

---

## 五、Linux 特有注意事项

### 5.1 Wayland 显示问题

在 Wayland 桌面（Ubuntu 22.04+ 默认）下，如果遇到窗口显示异常：

```bash
GDK_BACKEND=x11 python3 run_app.py
```

### 5.2 远程使用（SSH X11 转发）

通过 SSH 远程使用需要 X11 转发：

```bash
# 连接时启用 X11 转发
ssh -X user@server

# 然后运行
cd /path/to/标本整理
python3 run_app.py
```

注意：远程模式下性能可能较差，图片加载会较慢。

### 5.3 文件权限

如果从 Windows 分区（NTFS/exFAT）复制的工作区，可能遇到文件权限问题：

```bash
# 修复工作区权限
chmod -R u+rw "/path/to/workspace/数据/"
```

### 5.4 TIFF 图片查看

Linux 系统默认图片查看器可能不支持 TIFF。推荐安装：

```bash
# Ubuntu / Debian
sudo apt install eog       # GNOME 图片查看器（支持 TIFF）

# 或安装 ImageMagick
sudo apt install imagemagick
```

软件内点击"打开原图"时，会调用系统默认图片查看器。如果默认查看器不支持 TIFF，可以右键照片文件设置默认打开方式。

### 5.5 高 DPI 显示

在高分辨率屏幕（4K）上，Tkinter 可能显示较小。可以通过环境变量调整：

```bash
# 强制 Tkinter 使用高 DPI 缩放
export TK_SCALE=1.5
python3 run_app.py
```

或设置 GDK 缩放：

```bash
export GDK_SCALE=2
export GDK_DPI_SCALE=0.5
python3 run_app.py
```

---

## 六、打包为可执行文件

如果需要将软件分发给没有 Python 环境的 Linux 用户：

### 6.1 安装 PyInstaller

```bash
pip3 install pyinstaller
```

### 6.2 构建

```bash
cd /home/用户名/标本整理
python3 build_release.py --version 0.2.4
```

### 6.3 打包分发

```bash
cd releases/v0.2.4/
tar czf 标本入库管理_v0.2.4_linux.tar.gz 标本入库管理_v0.2.4/
```

### 6.4 用户使用

接收方解压并运行：

```bash
tar xzf 标本入库管理_v0.2.4_linux.tar.gz
cd 标本入库管理_v0.2.4/
chmod +x 标本入库管理_v0.2.4
./标本入库管理_v0.2.4
```

### 6.5 多发行版兼容

为了兼容更多 Linux 发行版，建议在较旧的系统上构建（glibc 向后兼容）：

```bash
# 在 Ubuntu 20.04 上构建，可在 Ubuntu 20.04+ 上运行
# 使用 Docker 可以方便地模拟旧版本环境
docker run -it ubuntu:20.04 bash
# 在容器内安装 Python 和依赖，然后构建
```

---

## 七、故障排除

### 问题：`ModuleNotFoundError: No module named 'tkinter'`

Tkinter 未安装。参考 1.2 节安装 `python3-tk`。

### 问题：`_tkinter.TclError: no display name and no $DISPLAY environment variable`

没有图形桌面环境。需要：
- 在桌面环境中运行（不是纯终端）
- 远程时使用 `ssh -X`

### 问题：中文显示为方块

缺少中文字体。参考 1.3 节安装中文字体包。

### 问题：`ImportError: No module named 'openpyxl'`

未安装 Python 依赖。执行：

```bash
pip3 install -r requirements.txt
```

### 问题：照片拖放不工作

`tkinterdnd2` 未安装或安装失败。参考 1.6 节。不影响其他功能，可用"添加照片"按钮替代。

### 问题：大 TIFF 图片加载缓慢

首次加载大 TIFF 需要生成缩略图缓存，后续加载会很快。500MB+ TIFF 首次加载可能需要几秒。

### 问题：打包后运行报错 `libtk*.so not found`

PyInstaller 未正确打包 Tk 库。修改 `build_release.py`，在 PyInstaller 命令中添加：

```
--collect-all tkinter
```
