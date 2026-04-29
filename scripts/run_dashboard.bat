@echo off
REM NFL News Agent — Launch Streamlit Dashboard

cd /d "C:\Users\cwech\Documents\Claude\Projects\NFL_News_Agent"
call C:\Users\cwech\anaconda3\Scripts\activate.bat nfl_agent
streamlit run dashboard\app.py --server.port 8502
