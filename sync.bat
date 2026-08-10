@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   Sync Matinee Finder
echo   Brings this PC and the live site into line, both ways.
echo ============================================================
echo.

rem ---- is git usable at all? -------------------------------------------
git --version >nul 2>&1
if errorlevel 1 (
    echo   Git does not seem to be installed, or Windows cannot find it.
    echo   Nothing has been changed.
    echo.
    pause
    exit /b 1
)

rem ---- remember where the live site was, so we can report what arrived.
rem      Tracking origin rather than HEAD keeps your own commit from being
rem      reported back to you as though it were someone else's update.
for /f "delims=" %%i in ('git rev-parse origin/main') do set BEFORE=%%i

rem ---- do you have unpublished edits on this PC? -----------------------
set DIRTY=
for /f "delims=" %%i in ('git status --porcelain') do set DIRTY=1

set PUBLISH=
if defined DIRTY (
    echo   You have changes on this PC that are not on the live site yet:
    echo.
    git status --short
    echo.
    set /p ANSWER="  Publish these to the live site? (Y/N): "
    echo.
    rem accept y, Y, yes, Yep... - only the first letter is checked
    if /i "!ANSWER:~0,1!"=="Y" (
        set /p MSG="  Short description of the change [Enter for 'Update text']: "
        if "!MSG!"=="" set MSG=Update text
        rem 2>nul hides git's harmless "LF will be replaced by CRLF" chatter
        git add -A 2>nul
        git commit -m "!MSG!" >nul 2>nul
        if errorlevel 1 (
            echo   Could not save the change. Nothing published.
            echo.
            pause
            exit /b 1
        )
        echo   Saved locally. Will publish in a moment.
        set PUBLISH=1
    ) else (
        echo   Leaving your edits alone - just checking for updates.
        echo   NOTE: your PC will stay out of step until you publish or undo them.
        git fetch origin
        echo.
        git log --oneline HEAD..origin/main
        echo.
        echo   Done. Nothing was published.
        echo.
        pause
        exit /b 0
    )
    echo.
)

rem ---- bring down anything new (the daily robot commits every morning) --
echo   Checking for updates from the live site...
git pull --rebase
if errorlevel 1 (
    git rebase --abort >nul 2>&1
    echo.
    echo   Could not combine the changes automatically - the same lines were
    echo   edited both here and online.
    echo   Nothing is broken: your PC has been put back exactly as it was.
    echo   Easiest fix is to ask Claude to sort it out.
    echo.
    pause
    exit /b 1
)
echo.

rem ---- report what came down, BEFORE publishing. Pushing moves
rem      origin/main again, so measuring after the push would report your
rem      own commit back to you as an incoming update.
for /f "delims=" %%i in ('git rev-parse origin/main') do set AFTER=%%i
if "!BEFORE!"=="!AFTER!" (
    echo   Nothing new had been published elsewhere.
) else (
    echo   Came down from the live site:
    git log --oneline --no-decorate !BEFORE!..!AFTER!
)
echo.

rem ---- publish, if we committed something above -------------------------
if defined PUBLISH (
    echo   Publishing to the live site...
    git push
    if errorlevel 1 (
        echo.
        echo   Could not publish. You may be offline, or need to sign in to GitHub.
        echo   Your change is saved on this PC and nothing is lost - just run
        echo   this again when you are back online.
        echo.
        pause
        exit /b 1
    )
    echo.
    echo   Published. The live site updates in about a minute:
    echo     https://matinees.petersahui.com/
    echo.
)

echo   PC and live site are now in step.
echo.
pause
