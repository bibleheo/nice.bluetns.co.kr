#!/usr/bin/env bash
# 웹 UI를 안정적으로 (재)시작하는 스크립트.
#   사용:  bash run_web.sh
# 기존 streamlit을 정리하고 백그라운드로 새로 띄운 뒤 상태를 보여준다.
cd "$(dirname "$0")"

echo "[1/3] 기존 streamlit 정리..."
pkill -f streamlit 2>/dev/null || true
sleep 1

echo "[2/3] 웹앱 시작 (백그라운드)..."
nohup python -m streamlit run nice_bizline/app/web/streamlit_app.py > ~/st.log 2>&1 &
sleep 6

echo "[3/3] 상태 확인:"
echo "------------------------------------------------------------"
tail -6 ~/st.log
echo "------------------------------------------------------------"

# 로컬에서 실제로 응답하는지 확인
code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8501 || echo "000")
if [ "$code" = "200" ]; then
  echo "✅ streamlit 정상 (localhost 200)."
  echo "   → 아래쪽 [포트/PORTS] 탭에서 8501의 🌐 지구본을 클릭해 여세요."
  echo "   → 404가 나면 8501 우클릭 → Port Visibility → Public 후 다시 지구본."
else
  echo "❌ streamlit 미응답(code=$code). 잠시 뒤 다시:  cat ~/st.log"
fi
