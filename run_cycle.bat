@echo off
cd /d "%~dp0"
python main.py once >> logs\scheduled.log 2>&1
