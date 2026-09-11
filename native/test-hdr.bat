@echo off
setlocal
cd /d "%~dp0"
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
if not exist ..\_work mkdir ..\_work
cl /nologo /EHsc /W4 /std:c++17 ..\tests\hdr_gpu.cpp /Fo:..\_work\hdr_gpu.obj /Fe:..\_work\hdr_gpu.exe /link d3d11.lib d3dcompiler.lib user32.lib
if errorlevel 1 exit /b 1
..\_work\hdr_gpu.exe
exit /b %errorlevel%
