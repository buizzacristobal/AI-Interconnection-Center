@echo off
cd /d "%~dp0"
if "%AIC_PORT%"=="" set AIC_PORT=19843
start "" /min pythonw channel_viewer.py
timeout /t 2 >nul
start http://127.0.0.1:%AIC_PORT%
