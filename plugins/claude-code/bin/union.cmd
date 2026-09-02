@echo off
rem Union CLI wrapper for cmd.exe. Prefer python, fall back to the py launcher.
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if %errorlevel%==0 (
  python "%~dp0..\launch.py" %*
) else (
  py -3 "%~dp0..\launch.py" %*
)
