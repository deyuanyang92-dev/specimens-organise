# 标本入库管理 — Linux 使用指南

## 一、环境安装

### 1.1 系统要求

- Linux 桌面环境（X11 或 Wayland）
- **便携版**：无需 Python，下载 zip 解压即用
- **源码运行**：Python 3.10+，pip

### 1.2 便携版安装（推荐）

前往 [Releases](https://github.com/deyuanyang92-dev/specimens-organise/releases) 下载最新 `_linux.zip`，解压后：

```bash
chmod +x 标本入库管理_v0.4.0
./标本入库管理_v0.4.0
```

`.exe` 旁的 `_internal/` 目录必须保留在同一层，不能单独移动主程序文件。

### 1.3 源码安装

```bash
# 安装依赖（PyQt5 自带 Qt 运行时，无需安装系统级 GUI 包）
pip3 install -r requirements.txt

# 启动
python3 run_app.py
```

缺少依赖时软件会显示：

```
[错误] 缺少依赖库：No module named 'PyQt5'
请运行：  pip install -r requirements.txt
```

### 1.4 中文字体

界面中文显示为方块时，安装中文字体：

```bash
# Ubuntu / Debian
sudo apt install fonts-noto-cjk

# Fedora
sudo dnf install google-noto-sans-cjk-fonts

# Arch Linux
sudo pacman -S noto-fonts-cjk
```

### 1.5 获取项目代码

```bash
git clone https://github.com/deyuanyang92-dev/specimens-organise.git
cd specimens-organise
```

---

## 二、启动与工作区

### 2.1 启动

```bash
python3 run_app.py

# 或指定工作区
python3 run_app.py --workspace "/path/to/my-workspace"
```

桌面快捷方式（可选）：

```bash
cat > ~/桌面/标本入库管理.sh << 'EOF'
#!/bin/bash
cd "/path/to/specimens-organise"
python3 run_app.py &
EOF
chmod +x ~/桌面/标本入库管理.sh
```

### 2.2 工作区概念

**工作区**是存放所有标本数据的目录。每个工作区包含：

```
我的工作区/
└── 数据/                    # 所有标本数据（自动创建）
    ├── 标本信息.xlsx
    ├── 照片信息.xlsx
    ├── 分类信息.xlsx
    ├── 编号索引.xlsx
    ├── 修改记录.xlsx
    ├── 操作记录.xlsx
    ├── 工作区配置.json
    ├── 缩略图缓存/
    └── 数据版本/            # 快照备份数据
```

**关键点：**
- 外部照片默认复制进工作区 `照片/`，原文件不变
- 工作区可整体移动/备份，数据不丢失
- 不要使用 `build/`、`dist/`、`releases/` 目录作为工作区

### 2.3 自动记忆

软件会记住上次使用的工作区，下次启动时自动打开。

### 2.4 初始化新工作区

选择空目录时，软件弹窗确认后自动创建 `数据/` 目录。

---

## 三、主要功能使用（v0.4.0）

### 3.1 新增入库编号

点击左侧面板顶部 **「＋新增入库编号」** 按钮，系统自动生成 `YZZ000001` 格式编号（自动递增）。

### 3.2 填写标本信息

选中左侧列表中的编号后，右侧出现三个信息面板。

**字段填写说明**：每个输入框后有一个低调的「?」按钮，hover 显示填写摘要，点击弹出完整说明。

**管内编号自动解析示例**：
- 输入 `QD-CK-SC008-260827`
- 自动填充采集地点缩写：`QD-CK-SC008`
- 自动填充采集日期：`2026-08-27`

**自动保存**：默认开启，输入停 0.5 秒自动写入。工具栏「自动保存：开/关」可切换；关闭时通过面板底部「保存」按钮手动写入。

### 3.3 填写分类信息

输入种名/科名片段后，自动从内置分类预设中匹配候选（无需手动维护预设文件）：

- 选中候选后自动填充种拉丁名、属名、科名、科拉丁名等
- 科名字段同样支持搜索补全

### 3.4 管理照片

#### 添加照片

- **按钮**：点击照片预览区上方「添加照片」
- **图片检索**：点击「检索图片」，在工作区内按编号/地点搜索，Ctrl/Shift 多选后「关联选中」

#### 取消关联

右键照片或选中后点「取消关联」：仅删除软件记录，原文件不受影响。

#### 照片预览

- **宫格模式**：右上角下拉选 2/4/6/8 格；双击某格放大为单图
- **单图模式**：双击返回宫格；`Ctrl+滚轮` 缩放；滚轮切图；拖拽平移
- **右键菜单**：打开原图、打开所在目录、复制路径

### 3.5 入库汇总

工具栏「入库汇总」打开宽表视图：

- 支持 Ctrl/Shift 多选行
- 右键 → 「导出选中照片」：导出已选编号的关联照片（支持格式转换）
- 顶部搜索栏 → 「导入编号列表」：从文件/粘贴导入编号列表批量筛选

### 3.6 外观设置

菜单 → 设置：

- **应用图标**：4 款变体（标本蓝/账本绿/照片珊瑚/归档靛蓝）
- **光标样式**：6 种（默认/食指/手掌/钢笔/爪印/星星）
- **界面字体大小**：`Ctrl++`/`Ctrl+-`/`Ctrl+0` 也可快速调节

### 3.7 撤回与重做

工具栏「撤回」/「重做」，支持字段/照片/新建/删除，默认 200 步。

---

## 四、数据文件说明

| 文件 | 内容 |
|------|------|
| `标本信息.xlsx` | 标本基本字段 |
| `照片信息.xlsx` | 照片关联记录 |
| `分类信息.xlsx` | 分类字段 |
| `编号索引.xlsx` | 编号索引和指纹 |
| `修改记录.xlsx` | 字段修改明细和汇总 |
| `操作记录.xlsx` | 操作日志（撤回/重做） |
| `数据版本记录.xlsx` | 快照版本历史 |
| `工作区配置.json` | 工作区元信息 |
| `缩略图缓存/` | 自动生成的 JPEG 缩略图 |
| `数据版本/` | 快照备份数据 |

---

## 五、Linux 特有注意事项

### 5.1 Wayland 显示问题

在 Wayland 桌面下窗口显示异常时：

```bash
QT_QPA_PLATFORM=xcb python3 run_app.py
```

### 5.2 远程使用（SSH X11 转发）

```bash
ssh -X user@server
python3 run_app.py
```

### 5.3 文件权限

从 Windows 分区（NTFS/exFAT）复制的工作区可能有权限问题：

```bash
chmod -R u+rw "/path/to/workspace/数据/"
```

### 5.4 TIFF 图片查看

系统默认图片查看器可能不支持 TIFF，推荐：

```bash
# Ubuntu / Debian
sudo apt install eog        # GNOME 图片查看器
```

### 5.5 高 DPI 显示

在高分辨率屏幕上 Qt 可能显示较小：

```bash
export QT_SCALE_FACTOR=1.5
python3 run_app.py
```

---

## 六、故障排除

**`No module named 'PyQt5'` 等 ModuleNotFoundError**

```bash
pip3 install -r requirements.txt
```

**`qt.qpa.plugin: Could not load the Qt platform plugin "xcb"`**

```bash
# Debian/Ubuntu
sudo apt install libxcb-xinerama0 libxcb-icccm4 libxcb-image0 \
    libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-xkb1 libxkbcommon-x11-0
```

**中文显示为方块**

参考 1.4 节安装中文字体包。

**大 TIFF 图片加载缓慢**

首次加载需生成缩略图缓存，后续会很快。500MB+ TIFF 首次可能需要数秒。

**分类自动匹配不出现候选**

分类预设已内置于软件，无需手动放置文件，直接输入种名/科名片段即可触发。
