@echo off
rem Build token-chart.exe (standalone, no Python needed on the target PC).
cd /d "%~dp0"
where python >nul 2>nul || (
  echo Python is required to BUILD the exe. See python.org.
  pause & exit /b 1
)
echo [1/2] installing pyinstaller ...
python -m pip install --quiet --upgrade pyinstaller
echo [2/2] building ...
python -m PyInstaller --onefile --name token-chart --distpath . --workpath ..\tmp\pyi-build --specpath ..\tmp\pyi-build --noconfirm build.py
echo.
if exist token-chart.exe (
  echo Done: token-chart.exe
) else (
  echo Build failed.
)
pause
