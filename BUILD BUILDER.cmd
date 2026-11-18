@echo off
rem Builder: build, self-test, sign with the Cydcor certificate, package and verify.
rem Copy this folder to C:\builder first (elevated windows cannot see network drives).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\windows\build_signed_installer.ps1" %*
