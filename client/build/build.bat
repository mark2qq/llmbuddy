@echo off
chcp 65001 >nul
REM ============================================================
REM  一键构建脚本（client/build/build.bat）
REM  流程：PyInstaller 生成 onedir 产物 -> Inno Setup 编译安装包
REM  环境要求：
REM    - Python 3.11（含 pyinstaller / PySide6 / fastapi / uvicorn / httpx / psutil）
REM    - Inno Setup 6（ISCC.exe 路径见下方，按实际安装位置修改）
REM ============================================================
REM 切到仓库根目录（本脚本位于 <repo>\client\build，故上溯两级）
REM 必须从仓库根执行：PyInstaller 的 --distpath/--workpath 是相对当前目录的，
REM 产物需落在 <repo>\dist\llmBuddy，inno_setup.iss 里的 DistDir 也指向该路径。
cd /d %~dp0\..\..

set PYTHON=C:\Program Files\Python311\python.exe
REM Inno Setup 6 通常装在当前用户目录；Program Files 下的路径作为兜底
set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set ISCC=C:\Program Files\Inno Setup 6\ISCC.exe

REM 注意：PyInstaller 的 COLLECT 阶段会整目录删除已存在的 dist\llmBuddy。
REM 若删除被拦截/失败，可先把旧产物改名留存（本项目历史做法）：
REM   move dist\llmBuddy dist\llmBuddy.bak-%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%
echo [1/3] 正在用 PyInstaller 构建 onedir 产物 ...
%PYTHON% -m PyInstaller client\build\pyinstaller.spec --noconfirm --clean --distpath dist --workpath build\pyi
if errorlevel 1 (
    echo [错误] PyInstaller 构建失败，请检查上方错误信息。
    pause
    exit /b 1
)
if not exist dist\llmBuddy\llmBuddy.exe (
    echo [错误] 未找到产物 dist\llmBuddy\llmBuddy.exe
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
echo   - onedir 产物：dist\llmBuddy\
echo   - 安装包：client\build\installer\llmBuddy-Setup-1.0.5.exe
echo.
pause
