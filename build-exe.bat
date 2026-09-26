@echo off
rem Construit rdlab.exe : un fichier unique, sans Python a installer.
rem A lancer sur une machine Windows disposant du projet installe
rem (installer-windows.bat lance au moins une fois).
rem
rem Ce fichier DOIT rester en fins de ligne CRLF, et sans bloc if(...)
rem multi-ligne : cmd.exe ne sait pas les interpreter en LF seul.
setlocal
cd /d "%~dp0"
title Construction de rdlab.exe

echo.
echo  ============================================================
echo   Construction de rdlab.exe
echo  ============================================================
echo.

if not exist ".venv\Scripts\python.exe" goto pas_de_venv

echo  [1/2] Installation de PyInstaller si necessaire...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pyinstaller
if errorlevel 1 goto echec_pip

echo  [2/2] Compilation (3 a 5 minutes, soyez patient)...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean rdlab.spec
if errorlevel 1 goto echec_build

echo.
echo  ============================================================
echo   Termine.
echo.
echo   Votre executable :  dist\rdlab.exe
echo.
echo   Copiez CE SEUL FICHIER sur l'autre PC et double-cliquez.
echo   Aucun Python n'est requis sur la machine de destination.
echo  ============================================================
echo.
pause
goto fin

:pas_de_venv
echo.
echo   Environnement absent. Lancez d'abord installer-windows.bat
echo.
pause
goto fin

:echec_pip
echo.
echo   Echec de l'installation de PyInstaller. Verifiez votre connexion.
echo.
pause
goto fin

:echec_build
echo.
echo   La compilation a echoue. Le detail est au-dessus.
echo.
pause

:fin
