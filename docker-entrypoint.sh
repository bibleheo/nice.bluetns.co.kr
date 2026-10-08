#!/bin/sh
# 컨테이너는 root 로 시작하되, /data(공유 볼륨) 소유권만 맞춘 뒤
# 일반 사용자(app, uid 1000)로 권한을 내려서 실제 프로세스를 실행한다.
# 이미 존재하는 root 소유 볼륨도 첫 기동 때 한 번 app 소유로 바뀐다.
set -e

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"

if [ "$(id -u)" = "0" ]; then
    if [ "$(stat -c %u "$DATA_DIR")" != "$(id -u app)" ]; then
        chown -R app:app "$DATA_DIR"
    fi
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi

exec "$@"
