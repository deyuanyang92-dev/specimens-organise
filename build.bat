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

set PYTHONUTF8=1
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

for /f %%v in ('python -c "from specimen_app import __version__; print(__version__)"') do set APPVER=%%v

:: ---- 试跑打包好的 exe（2026-10-03：与 GitHub 自动打包同一道检查，打坏的包不要拿去用）----
echo [检查] 试跑打包产物 --smoke ...
set SMOKE_EXE=
for /r "releases\v%APPVER%" %%f in (*.exe) do (
    echo %%~nxf | findstr /b /c:"installer_" >nul || set "SMOKE_EXE=%%f"
)
if not defined SMOKE_EXE (
    echo [警告] 未找到打包好的 exe，跳过试跑
) else (
    set QT_QPA_PLATFORM=offscreen
    rem GUI 程序直接调用不会等它结束，必须 start /wait 才拿得到退出码
    start "" /wait "%SMOKE_EXE%" --smoke
    if errorlevel 1 (
        echo [错误] 打包产物试跑失败，日志见 %%APPDATA%%\标本入库管理\startup_failure_*.log
        pause
        exit /b 1
    )
    set QT_QPA_PLATFORM=
)

echo.
echo ========================================
echo   构建成功！v%APPVER%
echo.
:: 旧：echo   输出目录: dist\标本入库管理\  —— build_release.py 实际输出在 releases\v版本号\
echo   输出目录: releases\v%APPVER%\
echo     installer_v%APPVER%_windows.exe  安装器（需装 Inno Setup 6 才会生成）
echo     setup_v%APPVER%_windows.zip      便携版（整体解压运行）
echo ========================================
echo.
echo 按任意键打开输出目录...
pause >nul
explorer "releases\v%APPVER%"
