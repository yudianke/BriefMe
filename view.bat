@echo off
cd /d "%~dp0"

if not exist "output\index.html" goto :nofile

echo 正在打开每日新闻...
start "" "output\index.html"
exit /b 0

:nofile
echo.
echo   还没有生成网页，请先运行其中一种：
echo.
echo     python run.py            用 API 额度，全自动
echo     python run.py --manual   不用额度，导出待办给 Claude Code
echo.
pause
exit /b 1
