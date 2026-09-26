@echo off
rem Installe rdlab sur cette machine Windows : environnement Python isole
rem + dependances. A lancer une seule fois, par double-clic.
rem
rem Ce fichier DOIT rester en fins de ligne CRLF, et sans bloc if(...)
rem multi-ligne : cmd.exe ne sait pas les interpreter en LF seul.
setlocal
cd /d "%~dp0"
title Installation de rdlab

echo.
echo  ============================================================
echo   Installation de rdlab
echo  ============================================================
echo.

rem --- 1. Python present ? -----------------------------------------
echo  [1/4] Recherche de Python...
python --version >nul 2>&1
if errorlevel 1 goto pas_de_python
for /f "tokens=*" %%v in ('python --version') do echo        %%v
python -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)"
if errorlevel 1 goto version_trop_vieille

rem --- 2. tkinter present ? ----------------------------------------
echo  [2/4] Verification de l'interface graphique (tkinter)...
python -c "import tkinter" >nul 2>&1
if errorlevel 1 goto pas_de_tkinter
echo        tkinter disponible

rem --- 3. environnement isole --------------------------------------
echo  [3/4] Creation de l'environnement Python (.venv)...
if exist ".venv\Scripts\python.exe" goto venv_pret
python -m venv .venv
if errorlevel 1 goto echec_venv
:venv_pret
echo        pret

rem --- 4. dependances ----------------------------------------------
echo  [4/4] Installation des dependances (1 a 2 minutes)...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto echec_pip
".venv\Scripts\python.exe" -c "import cryptography,mss,PIL,numpy,pynput,tkinter" >nul 2>&1
if errorlevel 1 goto echec_import

echo.
echo  ============================================================
echo   Installation terminee.
echo.
echo   Lancez rdlab avec :  lancer-rdlab.bat
echo  ============================================================
echo.
choice /c ON /n /m "  Lancer rdlab maintenant ? [O/N] "
if errorlevel 2 goto fin
start "" "%~dp0lancer-rdlab.bat"
goto fin

:pas_de_python
echo.
echo   Python n'est pas installe, ou absent du PATH.
echo.
echo   1. Telechargez-le sur  https://www.python.org/downloads/
echo   2. IMPORTANT : cochez "Add python.exe to PATH" sur le premier ecran
echo   3. Relancez ce fichier
echo.
goto pause_fin

:version_trop_vieille
echo.
echo   Python 3.9 minimum est requis. Mettez a jour depuis python.org.
echo.
goto pause_fin

:pas_de_tkinter
echo.
echo   tkinter est absent. Reinstallez Python depuis python.org en
echo   laissant cochee l'option "tcl/tk and IDLE".
echo.
goto pause_fin

:echec_venv
echo.
echo   Echec de la creation de l'environnement. Si le chemin de ce
echo   dossier est tres long, deplacez-le vers C:\rdlab et reessayez.
echo.
goto pause_fin

:echec_pip
echo.
echo   Echec de l'installation des dependances. Verifiez votre
echo   connexion Internet, puis relancez ce fichier.
echo.
goto pause_fin

:echec_import
echo.
echo   Les dependances se sont installees mais ne se chargent pas.
echo   Detail :
echo.
".venv\Scripts\python.exe" -c "import cryptography,mss,PIL,numpy,pynput,tkinter"
echo.
goto pause_fin

:pause_fin
pause

:fin
