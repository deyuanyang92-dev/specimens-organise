@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo ========================================
echo   标本入库管理 - Windows 一键打包
echo ========================================
echo.

:: ---- 检查 Python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python！
    echo.
    echo 请按以下步骤安装：
    echo   1. 访问 https://www.python.org/downloads/
    echo   2. 下载 Python 3.10 或更高版本
    echo   3. 安装时务必勾选 "Add Python to PATH"
    echo.
    start https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [信息] Python %PYVER%
echo.

:: ---- 安装依赖 ----
echo [1/3] 安装项目依赖...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络连接
    pause
    exit /b 1
)

pip install pyinstaller -q
if errorlevel 1 (
    echo [错误] PyInstaller 安装失败
    pause
    exit /b 1
)

:: ---- 验证导入 ----
echo [2/3] 验证程序...
python -c "from specimen_app import __version__; print(f'  版本: {__version__}')"
if errorlevel 1 (
    echo [错误] 程序验证失败，请检查代码完整性
    pause
    exit /b 1
)

:: ---- 构建 ----
echo [3/3] 构建 EXE（首次构建约需 1-3 分钟）...
echo.
python build_release.py
if errorlevel 1 (
    echo.
    echo [错误] 构建失败，请查看上方错误信息
    pause
    exit /b 1
)

echo.
echo ========================================
echo   构建成功！
echo.
echo   输出目录: dist\标本入库管理\
echo   分发方式: 将该目录打包为 ZIP
echo ========================================
echo.
echo 按任意键打开输出目录...
pause >nul
explorer "dist\标本入库管理"
