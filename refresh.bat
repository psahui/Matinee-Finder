@echo off
rem Sydney Matinee Finder - one-click refresh
rem Runs the scraper (about 4 minutes) and opens the results in your browser.

cd /d "%~dp0"

echo ============================================================
echo  Refreshing Sydney matinee listings - takes about 4 minutes.
echo  Progress for each source will appear below.
echo ============================================================
echo.

python sydney_matinee_finder.py

if errorlevel 1 (
    echo.
    echo Something went wrong - see the messages above.
    pause
    exit /b 1
)

start "" "sydney_matinees.html"

echo.
echo Opened sydney_matinees.html in your browser.
pause
