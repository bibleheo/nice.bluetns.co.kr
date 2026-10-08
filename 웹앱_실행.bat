@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 나이스비즈라인 조회 - 로컬 웹앱
echo ============================================
echo   나이스비즈라인 조회 - 로컬 웹앱 실행
echo ============================================
echo.

rem -- 파이썬 찾기: python 우선, 없으면 py -3 --
set "PY=python"
%PY% --version >nul 2>&1
if not errorlevel 1 goto :found
set "PY=py -3"
%PY% --version >nul 2>&1
if not errorlevel 1 goto :found
echo [오류] Python 이 설치되어 있지 않습니다.
echo   https://www.python.org/downloads/ 에서 Python 3.12 설치
echo   설치 시 Add python.exe to PATH 체크 필수!
echo.
pause
exit /b 1

:found
echo 사용 파이썬:
%PY% --version
echo.
echo [1/3] 패키지 설치/확인... 처음 한 번만 오래 걸립니다.
%PY% -m pip install -q -r requirements.txt
if errorlevel 1 goto :piperr

echo [2/3] 크롬 브라우저 설치/확인... 처음 한 번만.
%PY% -m playwright install chromium

echo [3/3] 웹앱 시작 - 잠시 후 브라우저가 자동으로 열립니다.
echo        종료하려면 이 검은 창을 닫으세요.
echo.
%PY% -m streamlit run nice_bizline/app/web/streamlit_app.py
pause
exit /b 0

:piperr
echo [오류] 패키지 설치 실패. 인터넷 연결을 확인하세요.
echo   Python 3.11 또는 3.12 를 권장합니다.
pause
exit /b 1
