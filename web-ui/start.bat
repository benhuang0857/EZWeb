@echo off
cd /d %~dp0
py -3 -m uvicorn main:app --host 0.0.0.0 --port 3000 --reload
