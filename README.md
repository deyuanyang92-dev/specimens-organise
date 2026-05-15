# 标本入库管理

生物标本入库工作台，基于 PyQt5，数据以 Excel 格式存储在工作区目录中。支持 Windows 和 Linux。

**当前版本：v0.4.0**

## 功能亮点

- 标本录入：入库编号自动生成、管内编号解析采集日期/地点、字段填写说明（输入框旁「?」提示）
- 分类信息：种名/科名输入时自动匹配预设分类系统，无需手动翻表
- 照片管理：批量关联、取消关联（原文件不删除）、格式转换导出（JPG/PNG/TIFF）
- 入库汇总：宽表视图，多选导出照片，导入编号列表筛选
- 自动保存：输入停 0.5s 自动写入（工具栏可关闭）
- 外观定制：4 款应用图标变体、6 种趣味光标、字体大小调节
- 撤回/重做：200 步历史，支持字段/照片/新建/删除
- 版本管理：工作区数据快照、GitHub 在线检查更新

---

## 快速开始

### 方式一：免安装便携版（推荐）

无需安装 Python，下载即用。

**Windows（Win 10/11 64位）**

1. 前往 [Releases 页面](https://github.com/deyuanyang92-dev/specimens-organise/releases) 下载最新版 `标本入库管理_vX.X.X_windows.zip`（或 `_v00X_windows.zip`）
2. 解压到任意目录
3. 双击 `标本入库管理_vX.X.X.exe` 启动

> **注意**：软件采用目录模式（`--onedir`）打包，`.exe` 文件必须和 `_internal/` 子目录放在同一层，不能单独复制 `.exe`。

**Linux（Ubuntu 20.04+）**

1. 下载 `_linux.zip`，解压
2. 进入解压后的目录，添加执行权限并运行：

```bash
chmod +x 标本入库管理_vX.X.X
./标本入库管理_vX.X.X
```

---

### 方式二：从源码运行（开发 / 高级用户）

**前提：Python 3.10 或更高版本**

```bash
# 1. 克隆仓库
git clone https://github.com/deyuanyang92-dev/specimens-organise.git
cd specimens-organise

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动
python run_app.py
```

**Windows PowerShell 示例：**

```powershell
pip install -r requirements.txt
python .\run_app.py
```

缺少依赖时，软件会显示明确提示：

```
[错误] 缺少依赖库：No module named 'PyQt5'
请运行：  pip install -r requirements.txt
```

**指定工作区启动：**

```bash
python run_app.py --workspace "D:\我的标本数据"
```

---

## 系统要求

|  | Windows | Linux |
|--|---------|-------|
| **便携版** | Win 10/11 64位，无需 Python | Ubuntu 20.04+ / 等效发行版 |
| **源码运行** | Python 3.10+，pip | Python 3.10+，pip |
| **依赖** | PyQt5 / openpyxl / Pillow / tifffile | 同左 |

---

## 工作区

软件以「工作区文件夹」为单位存储数据。首次启动会自动定位（当前目录或上次打开的位置），也可在工具栏手动切换或创建新工作区。

**目录结构：**

```
我的工作区/
└── 数据/                      # 所有标本数据（自动创建）
    ├── 标本信息.xlsx
    ├── 照片信息.xlsx
    ├── 分类信息.xlsx
    ├── 编号索引.xlsx
    ├── 修改记录.xlsx
    ├── 操作记录.xlsx
    ├── 工作区配置.json
    ├── 缩略图缓存/
    └── 数据版本/              # 快照备份
```

- 工作区可整体移动/复制/备份，数据不丢失
- 外部照片默认复制进工作区 `照片/`，原文件不变
- 不要将 `build/`、`dist/`、`releases/` 目录当作工作区

---

## 常见问题

**Q：运行 `python run_app.py` 报 `ModuleNotFoundError`**

```bash
pip install -r requirements.txt
```

**Q：双击 EXE 没有反应 / 提示缺少 DLL**

确认 `.exe` 文件和 `_internal/` 子目录在同一目录下。软件是目录模式打包，不能单独运行 `.exe`。

**Q：种名/科名输入后没有自动补全**

分类预设已内置在软件中（`字段模版/` 随软件分发），无需手动放置任何文件，直接输入即可触发补全。

**Q：照片「取消关联」会删除原文件吗**

不会。「取消关联」只删除软件中的记录（`照片信息.xlsx` 里的行），以及工作区 `照片/` 中的归档副本（如果没有其他记录引用它）。用户原始文件从不被删除。

**Q：Linux 上显示异常（Wayland）**

```bash
QT_QPA_PLATFORM=xcb python run_app.py
```

**Q：Linux 中文显示为方块**

```bash
# Ubuntu / Debian
sudo apt install fonts-noto-cjk
```

---

## 开发与构建

```bash
# 运行测试
python -m unittest discover -s tests

# 构建 Windows EXE
python build_release.py --version 0.4.0

# 一键构建（Windows，自动安装依赖）
build.bat
```

详见 [docs/build-windows.md](docs/build-windows.md) 和 [docs/build-linux.md](docs/build-linux.md)。
