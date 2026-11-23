@echo off
setlocal
pushd "%~dp0" || exit /b 1

python.exe -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>&1
if not errorlevel 1 (
    python.exe run_workbench.py %*
) else (
    py.exe -3 -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>&1
    if errorlevel 1 (
        echo Install Python 3.12 or later and make python.exe or py.exe available on PATH.
        popd
        pause
        exit /b 1
    )
    py.exe -3 run_workbench.py %*
)

set "workbench_exit=%errorlevel%"
popd
if not "%workbench_exit%"=="0" pause
exit /b %workbench_exit%
