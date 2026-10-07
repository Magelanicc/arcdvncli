@echo off
echo Mempersiapkan proses Build ARCDVNCLI menjadi .exe...

echo 1. Menginstal PyInstaller...
pip install pyinstaller

echo 2. Membangun file executable (standalone)...
pyinstaller --onefile --name arcdvncli --clean entry.py

echo.
echo Selesai! File arcdvncli.exe Anda sekarang berada di dalam folder "dist".
pause
