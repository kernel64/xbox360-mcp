@echo off
rem Watch the Xbox 360's HDMI output in VLC through the shared capture stream.
rem The agents keep capturing through the same stream: opening or closing VLC never disturbs them.
setlocal

rem the project folder is the parent of this script's folder
pushd "%~dp0.."

if not defined XBOX360_CAPTURE_PORT set XBOX360_CAPTURE_PORT=8360
set "URL=http://127.0.0.1:%XBOX360_CAPTURE_PORT%/xbox.ts"

rem find VLC
set "VLC="
if exist "%ProgramFiles%\VideoLAN\VLC\vlc.exe" set "VLC=%ProgramFiles%\VideoLAN\VLC\vlc.exe"
if not defined VLC if exist "%ProgramFiles(x86)%\VideoLAN\VLC\vlc.exe" set "VLC=%ProgramFiles(x86)%\VideoLAN\VLC\vlc.exe"
if not defined VLC for /f "delims=" %%v in ('where vlc 2^>nul') do if not defined VLC set "VLC=%%v"
if not defined VLC (
    echo VLC was not found. Install it from https://www.videolan.org/ or add vlc.exe to PATH.
    goto :fail
)

rem start the shared stream (or keep the running one alive while we watch)
echo Starting the shared capture stream...
call uv run xbox360 capture serve --idle 0
if errorlevel 1 (
    echo.
    echo The stream could not start. Is the capture card plugged in, and not open in OBS as "USB Video"?
    goto :fail
)

echo.
echo Opening %URL% in VLC. Close VLC when you are done.
rem --network-caching=300: about a third of a second of delay instead of VLC's default second
"%VLC%" --network-caching=300 --meta-title="Xbox 360" "%URL%"

rem VLC is closed: the agents may still use the stream, so don't stop it.
rem Let it stop by itself after 10 minutes without any client instead.
call uv run xbox360 capture serve --idle 600 >nul 2>&1
echo VLC closed. The stream stops by itself after 10 minutes without a client
echo (or now with: uv run xbox360 capture stop).
popd
endlocal
exit /b 0

:fail
popd
endlocal
pause
exit /b 1
