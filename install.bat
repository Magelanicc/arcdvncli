@echo off
echo Menginstal ARCDVNCLI...
where pipx >nul 2>nul
if %errorlevel%==0 (
    pipx install --force .
) else (
    echo pipx tidak ditemukan, memakai pip --user...
    pip install --user .
)
echo.
echo Selesai! Buka terminal BARU lalu ketik: arcdvncli
pause