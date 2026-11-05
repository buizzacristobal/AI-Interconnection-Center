@echo off
rem One-click start on Windows: runs the hub in the background and opens the dashboard.
cd /d "%~dp0"
where pythonw >nul 2>nul && (pythonw launch_viewer.py) || (python launch_viewer.py)
