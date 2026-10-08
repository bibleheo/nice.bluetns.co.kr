# nice-web / nice-worker 공용 이미지 (compose 에서 command 만 다르게 사용)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Seoul \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/home/app

# 실행 계정: 일반 사용자 app(uid 1000). 프로세스는 root 로 돌지 않는다.
RUN groupadd -g 1000 app && useradd -m -u 1000 -g app app

WORKDIR /app

# 의존성 먼저 (레이어 캐시). Chromium + 시스템 라이브러리는 root 로 설치하고
# 모두가 읽을 수 있게 둔다 (일반 사용자 app 이 실행).
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright \
 && rm -rf /var/lib/apt/lists/*

COPY . .
RUN chown -R app:app /app \
 && chmod +x /app/docker-entrypoint.sh \
 && mkdir -p /data && chown app:app /data

# 결과·체크포인트·스크린샷은 모두 /data (공유 볼륨) 아래에만 쓴다.
VOLUME ["/data"]

# 엔트리포인트가 /data 소유권을 맞춘 뒤 app 사용자로 권한을 내린다.
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# 기본은 웹. worker 는 compose 에서 command 로 바꾼다.
CMD ["python", "-m", "streamlit", "run", "nice_bizline/app/web/streamlit_app.py", \
     "--server.port=8501", "--server.address=0.0.0.0"]
