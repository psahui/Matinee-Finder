@echo off
rem Sydney Matinee Finder - refresh listings and preview locally.
rem
rem The page loads its data with fetch(), which browsers block on file://
rem URLs, so opening index.html by double-clicking shows an error. This
rem serves the folder over HTTP instead.

cd /d "%~dp0"

echo ============================================================
echo  Refreshing Sydney concert listings.
echo  First run takes a few minutes; later runs reuse the cache.
echo ============================================================
echo.

python fetch_events.py
if errorlevel 1 (
    echo.
    echo Refresh FAILED - see the messages above.
    echo Existing data was left untouched.
    pause
    exit /b 1
)

echo.
echo Starting local preview at http://localhost:8000/
echo Press Ctrl+C in this window when you are finished.
echo.

start "" http://localhost:8000/
python -m http.server 8000
