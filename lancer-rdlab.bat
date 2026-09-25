@echo off
rem Lance rdlab. Ce fichier DOIT rester en fins de ligne CRLF : cmd.exe
rem ne sait pas interpreter un bloc if(...) multi-ligne en LF seul.
setlocal
cd /d "%~dp0"

set "RDLAB_PY=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%RDLAB_PY%" set "RDLAB_PY=pythonw.exe"

rem pythonw n affiche aucune erreur : on verifie d abord que tout charge,
rem sinon un double-clic rate ne donnerait aucun retour a l utilisateur.
set "RDLAB_CHECK=%~dp0.venv\Scripts\python.exe"
if not exist "%RDLAB_CHECK%" set "RDLAB_CHECK=python.exe"
"%RDLAB_CHECK%" -c "import rdlab.app" 2>rdlab-lancement.log
if errorlevel 1 goto erreur
del rdlab-lancement.log 2>nul

start "" "%RDLAB_PY%" "%~dp0rdlab.pyw"
goto fin

:erreur
echo.
echo Impossible de demarrer rdlab. Detail :
echo.
type rdlab-lancement.log
echo.
echo Installez les dependances :
echo     python -m venv .venv
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause

:fin
