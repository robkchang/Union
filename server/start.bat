@echo off
rem Start the Union server. Pass extra flags through, e.g. start.bat --host 0.0.0.0
cd /d "%~dp0"
if exist "..\.venv\Scripts\python.exe" (
  "..\.venv\Scripts\python.exe" union_web.py %*
) else (
  python union_web.py %*
)
