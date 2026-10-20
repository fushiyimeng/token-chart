@echo off
rem Regenerate token-usage.html from data/ and open it.
rem Prefers token-chart.exe (no Python needed) if it sits next to this file,
rem otherwise falls back to python build.py.
cd /d "%~dp0"

set RUN=
if exist "%~dp0token-chart.exe" set RUN="%~dp0token-chart.exe"
if not defined RUN (
  where python >nul 2>nul && set RUN=python build.py
)
if not defined RUN (
  echo.
  echo Neither token-chart.exe nor Python was found.
  echo Just open token-usage.html directly - it already has the data inside.
  pause
  exit /b 1
)

echo [1/3] merging data ...
%RUN%
if errorlevel 1 goto fail

echo.
echo [2/3] data folder now has:
for %%f in (data\*.csv) do echo    %%~nxf
if exist "data\ledger.txt" echo    ledger.txt
echo.
echo [3/3] opening page ...
start "" "%~dp0token-usage.html"
exit /b 0

:fail
echo.
echo Build failed. See the error above.
pause
exit /b 1
