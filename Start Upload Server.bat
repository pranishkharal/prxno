@echo off
echo Starting upload server on port 8766...
cd /d "D:\Auto Clips for Kick"
.venv\Scripts\python.exe -m uvicorn upload_server:app --host 0.0.0.0 --port 8766
pause
