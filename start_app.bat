@echo off
title WLO Compound Planner
cd /d "%~dp0"
python wlo_app.py %*
if errorlevel 1 pause
