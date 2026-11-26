@echo off
rem vCCA 网页前端（本机）：双击运行，浏览器打开 http://127.0.0.1:8770/ ；关闭窗口或 Ctrl+C 停止。
rem 第一次运行若没有账号，会提示创建（也可运行 fr_venv\Scripts\python.exe vcca_plan.py web-user set 名字）。
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0fr_venv\Scripts\python.exe" "%~dp0vcca_plan.py" serve %*
if errorlevel 1 pause
