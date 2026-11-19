#!/usr/bin/env bash
# nice 모듈 보안 점검 (포털 보안 점검 브리프 대응). 서버에서 배포 디렉터리 기준으로 실행:
#   cd /srv/nice.bluetns.co.kr && bash scripts/security_check.sh
# 읽기만 하고 아무것도 바꾸지 않는다. 비밀값은 어떤 경우에도 출력하지 않는다
# (비밀번호 검색은 nice-worker 컨테이너 안에서 환경변수로만 비교하고, 건수·파일 경로만 보여 준다).
set -u
cd "$(dirname "$0")/.." || exit 1
hr() { printf '\n== %s\n' "$1"; }

hr "1. 호스트 포트: nice 컨테이너가 연 포트 (비어 있어야 정상)"
docker ps --filter "name=nice" --format '{{.Names}}  ports=[{{.Ports}}]'

hr "2. 이 계정(\$USER=${USER:-?})에 등록된 SSH 키 지문 (배포 키 1개만 있어야 정상)"
if [ -f "$HOME/.ssh/authorized_keys" ]; then
  ssh-keygen -lf "$HOME/.ssh/authorized_keys" 2>&1
else
  echo "authorized_keys 없음"
fi

hr "3. .env 권한 (600, 소유자만 읽기여야 정상)"
if [ -f .env ]; then stat -c '%a %U:%G %n' .env; else echo ".env 없음"; fi

hr "4. 이미지 안의 비밀값 (.env 가 이미지에 없어야 정상)"
docker run --rm --entrypoint sh nice-bizline:latest -c \
  'if [ -e /app/.env ]; then echo "경고: 이미지 안에 /app/.env 있음"; else echo "정상: 이미지 안에 .env 없음"; fi'
echo "이미지 빌드 기록에 NICE_ 변수 언급: $(docker history --no-trunc nice-bizline:latest 2>/dev/null | grep -c 'NICE_')건 (0이어야 정상)"

hr "5. 데이터·로그 속 계정 값 (모두 0이어야 정상)"
docker compose exec -T nice-worker sh -c '
  n=0; [ -n "${NICE_PW:-}" ] && n=$(grep -rlF -- "$NICE_PW" /data 2>/dev/null | wc -l)
  echo "비밀번호가 들어간 파일: ${n}개"
  [ "$n" != "0" ] && grep -rlF -- "$NICE_PW" /data 2>/dev/null | head -20
  m=0; [ -n "${NICE_ID:-}" ] && m=$(grep -rlF -- "$NICE_ID" /data 2>/dev/null | wc -l)
  echo "계정 ID가 들어간 파일: ${m}개"
  [ "$m" != "0" ] && grep -rlF -- "$NICE_ID" /data 2>/dev/null | head -20
  f=$(grep -rl "fill(" /data/jobs/*/log.txt 2>/dev/null | wc -l)
  echo "입력 오류 문구(fill)가 남은 작업 로그: ${f}개"
'
docker compose logs --no-color nice-worker nice-web 2>/dev/null \
  | docker compose exec -T nice-worker sh -c '
      cat > /tmp/.nice_logs
      a=0; [ -n "${NICE_PW:-}" ] && a=$(grep -cF -- "$NICE_PW" /tmp/.nice_logs)
      b=0; [ -n "${NICE_ID:-}" ] && b=$(grep -cF -- "$NICE_ID" /tmp/.nice_logs)
      echo "컨테이너 로그 속 비밀번호: ${a}줄, 계정 ID: ${b}줄"
      rm -f /tmp/.nice_logs'

hr "6. 실패 스크린샷 (debug/) — 7일 지나면 자동 삭제"
docker compose exec -T nice-worker sh -c 'ls /data/debug 2>/dev/null | wc -l | xargs echo "남은 스크린샷:"'

hr "7. 이용 기록 요약 (계정 탈취 흔적)"
docker compose exec -T nice-web python -m nice_bizline.app.server.audit

echo
echo "점검 끝. 경고가 있으면 결과를 그대로 포털팀에 전달하세요 (비밀값은 출력되지 않습니다)."
