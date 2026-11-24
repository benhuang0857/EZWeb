@echo off
setlocal DisableDelayedExpansion
set "project_name={{project_name}}"
set "r_project_name=%1"
set "site_port={{site_port}}"
set "r_site_port=%2"
set "sql_port={{sql_port}}"
set "r_sql_port=%3"
for /F "delims=" %%a in (.env.org) DO (
   set line=%%a
   setlocal EnableDelayedExpansion
   set "line=!line:%project_name%=%r_project_name%!"
   set "line=!line:%site_port%=%r_site_port%!"
   set "line=!line:%sql_port%=%r_sql_port%!"
   >> .env echo(!line!
   endlocal
)