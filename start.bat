@echo off
rem BookOCR launcher. The Python environment is only built once, afterwards
rem the GUI starts without any console window.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: creating the Python environment and installing packages...
  echo This may take several minutes, please wait.
  set "USEUV="
  where uv >nul 2>nul
  if not errorlevel 1 set "USEUV=1"
  if not defined USEUV (
    where python >nul 2>nul
    if not errorlevel 1 set "PYLAUNCH=python"
  )
  if not defined USEUV if not defined PYLAUNCH (
    echo [ERROR] Python was not found.
    echo Install Python 3.10+ from https://www.python.org/downloads/ and run this again.
    pause
    exit /b 1
  )
  if defined USEUV (
    uv venv .venv
    uv pip install --python .venv\Scripts\python.exe -r requirements.txt
  ) else (
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
  )
)
rem 版面模型默认启用：已有的 .venv 如果没有 rapid-layout，也要补装。
set "PY=.venv\Scripts\python.exe"
"%PY%" -c "import rapid_layout" >nul 2>nul
if errorlevel 1 (
  echo Installing optional layout model...
  where uv >nul 2>nul
  if not errorlevel 1 (
    uv pip install --python "%PY%" -r requirements-optional.txt
  ) else (
    "%PY%" -m pip install -r requirements-optional.txt
  )
  if errorlevel 1 (
    echo [WARN] Layout model install failed; BookOCR will run without it.
  )
)
rem find the real pythonw (the venv one is a trampoline that opens a console)
set "PYW="
".venv\Scripts\python.exe" -c "import sys,os;open('_pyw_path.txt','w').write(os.path.join(os.path.dirname(sys._base_executable),'pythonw.exe'))" >nul 2>nul
if exist "_pyw_path.txt" set /p PYW=<"_pyw_path.txt"
if exist "_pyw_path.txt" del "_pyw_path.txt"
if not defined PYW set "PYW=.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=.venv\Scripts\python.exe"
start "" "%PYW%" main.py
exit /b 0
