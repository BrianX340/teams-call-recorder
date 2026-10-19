# Compila GrabadorCalls.exe (un solo archivo, sin consola) en dist\
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m pip install -q -r requirements.txt pyinstaller
python -c "import grabador_calls as g; g.dibujar_icono(g.ROJO, 256).save('icono.ico', sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])"
python -m PyInstaller --noconfirm --onefile --windowed --name GrabadorCalls `
    --icon icono.ico --version-file version.txt `
    --hidden-import pystray._win32 --hidden-import pycaw.pycaw --hidden-import psutil `
    grabador_calls.py
