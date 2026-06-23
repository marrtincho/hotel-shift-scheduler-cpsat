@echo off
chcp 65001 >nul
title Hotel Manager — Instalacion automatica

echo.
echo  Hotel Manager - Instalacion Windows
echo  ====================================
echo.

REM Verificar Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python no encontrado.
    echo Descargalo desde https://www.python.org/downloads/
    echo Marca "Add Python to PATH" durante la instalacion.
    pause
    exit /b 1
)
echo [OK] Python encontrado.

REM Instalar dependencias
echo Instalando dependencias...
pip install flask openpyxl waitress --quiet
if errorlevel 1 (
    echo [ERROR] Fallo al instalar dependencias.
    pause
    exit /b 1
)
echo [OK] Dependencias instaladas.

REM Crear carpetas
if not exist "data" mkdir data
if not exist "static" mkdir static
echo [OK] Carpetas creadas.

REM Crear Iniciar_Servidor.bat
(
echo @echo off
echo chcp 65001 ^>nul
echo title Hotel Manager
echo cd /d "%%~dp0"
echo echo.
echo echo  Hotel Manager corriendo en http://localhost:5000
echo echo  Cierra esta ventana para detener el servidor.
echo echo.
echo python -m waitress --host=0.0.0.0 --port=5000 app:app
echo pause
) > "Iniciar_Servidor.bat"
echo [OK] Iniciar_Servidor.bat creado.

REM Descargar cloudflared
echo Descargando Cloudflare Tunnel...
powershell -Command "Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile 'cloudflared.exe'" >nul 2>&1
if exist "cloudflared.exe" (
    echo [OK] cloudflared.exe descargado.
) else (
    echo [AVISO] Descarga manual necesaria.
    echo Ve a: https://github.com/cloudflare/cloudflared/releases/latest
    echo Descarga cloudflared-windows-amd64.exe y renombralo a cloudflared.exe
)

REM Crear Tunel_Temporal.bat
(
echo @echo off
echo chcp 65001 ^>nul
echo title Hotel Manager - Tunel Cloudflare
echo echo.
echo echo  Generando URL publica temporal...
echo echo  Comparte la URL https://xxxx.trycloudflare.com con los empleados.
echo echo  Cierra esta ventana para cerrar el acceso externo.
echo echo.
echo cloudflared.exe tunnel --url http://localhost:5000
echo pause
) > "Tunel_Temporal.bat"
echo [OK] Tunel_Temporal.bat creado.

REM Crear acceso directo en escritorio
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Hotel Manager.lnk'); $s.TargetPath = '%~dp0Iniciar_Servidor.bat'; $s.WorkingDirectory = '%~dp0'; $s.Save()" >nul 2>&1
echo [OK] Acceso directo creado en el escritorio.

echo.
echo  Instalacion completada.
echo  =======================
echo.
echo  Archivos creados:
echo    Iniciar_Servidor.bat  - Arranca la app en http://localhost:5000
echo    Tunel_Temporal.bat    - URL publica para acceso externo
echo    Acceso directo en el escritorio
echo.
echo  PRIMEROS PASOS:
echo    1. Ejecuta Iniciar_Servidor.bat
echo    2. Prueba en tu PC: http://localhost:5000
echo    3. Para acceso externo: ejecuta Tunel_Temporal.bat
echo       y comparte la URL generada (https://xxxx.trycloudflare.com)
echo.
pause
