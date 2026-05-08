# Windows 安装与打包教程

## 一键打包（推荐）

将项目复制到 Windows 机器后，**双击 `build.bat`** 即可自动完成：

1. 检测 Python 环境
2. 安装全部依赖（PyQt5、openpyxl、Pillow、tifffile、PyInstaller）
3. 构建 EXE
4. 打开输出目录

构建完成后，将 `dist\标本入库管理\` 目录打包为 ZIP 分发即可。

---

## 手动安装

### 1. 安装 Python

下载 Python 3.10+：https://www.python.org/downloads/

**安装时务必勾选 "Add Python to PATH"。**

验证：

```cmd
python --version
pip --version
```

### 2. 安装依赖

在项目根目录打开终端（CMD 或 PowerShell）：

```cmd
pip install -r requirements.txt
```

`requirements.txt` 包含：

| 包名 | 用途 |
|------|------|
| `PyQt5` | GUI 界面框架 |
| `openpyxl` | Excel 文件读写 |
| `Pillow` | 图片处理（缩略图等） |
| `tifffile` | TIFF 大图支持 |

### 3. 验证运行

```cmd
python run_app.py
```

确认应用正常启动后，再进行打包。

---

## 手动打包 EXE

```cmd
pip install pyinstaller
python build_release.py
```

也可指定版本号：

```cmd
python build_release.py --version 0.2.4
```

## 构建产物

```
releases/v0.2.4/
  ├── 标本入库管理_v0.2.4/          # 完整程序目录
  │   ├── 标本入库管理_v0.2.4.exe   # 主程序
  │   ├── *.dll                     # 依赖库
  │   └── ...
  ├── build_info.json               # 构建信息
  ├── release_notes.md              # 发布说明
  └── sha256.txt                    # 校验文件

dist/标本入库管理/                    # 最新稳定版（同上目录结构的副本）
  └── 标本入库管理_v0.2.4.exe
```

## 分发

将 `dist\标本入库管理\` 整个目录压缩为 ZIP。用户解压后双击 EXE 即可运行，无需安装 Python。

## 常见问题

### 杀毒软件误报

PyInstaller 打包的 EXE 常被 Windows Defender 误报。解决方式：

- 在 Windows Defender 中将 EXE 所在目录添加为排除项
- 或使用代码签名证书签名

### 打包后启动慢

本项目使用 `--onedir` 模式（目录版），比 `--onefile` 启动快得多。`--onefile` 每次启动需要解压到临时目录，会明显变慢。

### 设置图标

准备 `.ico` 格式图标文件，修改 `build_release.py` 中的 PyInstaller 命令，添加 `--icon=图标.ico`。

### 减小体积

打包后约 100-200MB，主要是 Python 运行时和 PyQt5。可在 `build_release.py` 中添加排除：

```
--exclude-module matplotlib
--exclude-module scipy
--exclude-module notebook
```

### "python 不是内部命令"

说明 Python 未加入 PATH。解决方式：

- 重新运行 Python 安装程序，勾选 "Add Python to PATH"
- 或手动将 Python 安装目录和 `Scripts\` 子目录加入系统 PATH
