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

rem ---- remember where we started, so we can report what arrived --------
for /f "delims=" %%i in ('git rev-parse HEAD') do set BEFORE=%%i

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
        git add -A
        git commit -m "!MSG!" >nul
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

rem ---- what actually arrived? -------------------------------------------
for /f "delims=" %%i in ('git rev-parse HEAD') do set AFTER=%%i
if "%BEFORE%"=="%AFTER%" (
    echo   Already up to date - nothing new came down.
) else (
    echo   Updates received:
    git log --oneline %BEFORE%..%AFTER%
)

echo.
echo   PC and live site are now in step.
echo.
pause
