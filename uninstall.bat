@echo off
echo Menghapus ARCDVNCLI dari sistem (Full Uninstall)...

echo 1. Menghapus paket...
where pipx >nul 2>nul
if %errorlevel%==0 pipx uninstall arcdvncli
pip uninstall -y arcdvncli

echo 2. Menghapus konfigurasi global (API Key/Model/History)...
if exist "%USERPROFILE%\.arcdvncli_env" del /f /q "%USERPROFILE%\.arcdvncli_env"
if exist "%USERPROFILE%\.arcdvncli_history" del /f /q "%USERPROFILE%\.arcdvncli_history"
if exist "%USERPROFILE%\.arcdvncli_update_check" del /f /q "%USERPROFILE%\.arcdvncli_update_check"

echo 3. Membersihkan cache build lokal...
if exist "arcdvncli.egg-info" rmdir /s /q "arcdvncli.egg-info"
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

FOR /d /r . %%d in (__pycache__) DO (
    if exist "%%d" rmdir /s /q "%%d"
)

echo.
echo ARCDVNCLI beserta konfigurasi dan cache sudah dihapus.
pause