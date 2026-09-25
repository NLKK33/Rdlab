@echo off
rem Lance rdlab avec le venv local s'il existe, sinon le python du systeme.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" rdlab.pyw
) else (
    start "" pythonw rdlab.pyw
)
