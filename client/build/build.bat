@echo off
chcp 65001 >nul
REM ============================================================
REM  一键构建脚本（client/build/build.bat）
REM  流程：PyInstaller 生成 onedir 产物 -> Inno Setup 编译安装包
REM  环境要求：
REM    - Python 3.11（含 pyinstaller / PySide6 / fastapi / uvicorn / httpx / psutil）
REM    - Inno Setup 6（ISCC.exe 路径见下方，按实际安装位置修改）
REM ============================================================
cd /d %~dp0\..

set PYTHON=C:\Program Files\Python311\python.exe
set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe

echo [1/3] 正在用 PyInstaller 构建 onedir 产物 ...
%PYTHON% -m PyInstaller client\build\pyinstaller.spec --noconfirm --clean --distpath dist --workpath build\pyi
if errorlevel 1 (
    echo [错误] PyInstaller 构建失败，请检查上方错误信息。
    pause
    exit /b 1
)
if not exist dist\LLMDesktop\LLMDesktop.exe (
    echo [错误] 未找到产物 dist\LLMDesktop\LLMDesktop.exe
    pause
    exit /b 1
)

echo.
echo [2/3] 正在用 Inno Setup 编译安装包 ...
if not exist "%ISCC%" (
    echo [错误] 未找到 ISCC.exe：%ISCC%
    echo 请安装 Inno Setup 6（https://jrsoftware.org/isdl.php）或修改本脚本中的 ISCC 路径。
    pause
    exit /b 1
)
"%ISCC%" client\build\inno_setup.iss
if errorlevel 1 (
    echo [错误] Inno Setup 编译失败，请检查上方错误信息。
    pause
    exit /b 1
)

echo.
echo [3/3] 构建完成！
echo   - onedir 产物：dist\LLMDesktop\
echo   - 安装包：client\build\installer\llmBuddy-Setup-1.0.0.exe
echo.
pause
