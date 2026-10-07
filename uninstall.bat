@echo off
echo Menghapus ARCDVNCLI dari sistem (Full Uninstall)...

echo 1. Menghapus paket dari Python pip...
pip uninstall -y arcdvncli

echo 2. Menghapus konfigurasi global (API Key/Model)...
if exist "%USERPROFILE%\.arcdvncli_env" (
    del /f /q "%USERPROFILE%\.arcdvncli_env"
    echo   File konfigurasi terhapus.
)

echo 3. Membersihkan cache instalasi lokal (.egg-info, __pycache__, dll)...
if exist "arcdvncli.egg-info" rmdir /s /q "arcdvncli.egg-info"
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

FOR /d /r . %%d in (__pycache__) DO (
    if exist "%%d" rmdir /s /q "%%d"
)

echo.
echo ARCDVNCLI beserta seluruh file konfigurasi dan cache telah sukses dihapus sampai bersih.
pause
