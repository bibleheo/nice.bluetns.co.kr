# nice-web / nice-worker 공용 이미지 (compose 에서 command 만 다르게 사용)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Seoul \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 의존성 먼저 (레이어 캐시). Chromium + 시스템 라이브러리는 worker 가 쓰지만
# 이미지 하나로 두 서비스를 돌리므로 함께 설치한다.
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY . .

# 결과·체크포인트·스크린샷은 모두 /data (공유 볼륨) 아래에만 쓴다.
VOLUME ["/data"]

# 기본은 웹. worker 는 compose 에서 command 로 바꾼다.
CMD ["python", "-m", "streamlit", "run", "nice_bizline/app/web/streamlit_app.py", \
     "--server.port=8501", "--server.address=0.0.0.0"]
