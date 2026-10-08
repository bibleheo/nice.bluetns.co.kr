#!/usr/bin/env bash
# Codespace 생성 시 1회 자동 실행: 패키지 + 브라우저 + 시스템 라이브러리 설치
set -e

echo "[1/3] 파이썬 패키지 설치..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "[2/2] Playwright 크롬 + 시스템 라이브러리 설치..."
# --with-deps 가 브라우저(사용자 캐시) + apt 시스템 라이브러리를 한 번에 처리.
# (sudo python -m playwright 방식은 root가 사용자 site-packages를 못 봐서 실패하므로 사용 안 함)
python -m playwright install --with-deps chromium

echo "설치 완료. 웹앱 실행:  python -m streamlit run nice_bizline/app/web/streamlit_app.py"
