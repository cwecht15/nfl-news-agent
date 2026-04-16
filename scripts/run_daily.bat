@echo off
REM NFL News Agent — Daily Run Wrapper
REM Called by Windows Task Scheduler
REM Uses conda env Python directly so activate.bat isn't needed.

cd /d "C:\Users\cwech\Documents\Claude\Projects\NFL_News_Agent"
"C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe" scripts\run_daily.py
