# Codespace 사용법 (요약)

되다 안되다 하지 않게, **매번 이 순서**로만 하면 됩니다.

## 0. 코드스페이스를 멈췄다 다시 켰을 때 (첫 명령)
터미널에서:
```bash
git pull
```
> 설치된 패키지·브라우저는 그대로 남아있어요. 코드만 최신으로 받으면 됩니다.
> 혹시 브랜치가 main으로 바뀌어 있으면: `git checkout claude/nice-bizlin-app-t82c1b && git pull`

---

## 1. 방법 A — 터미널 실행 (제일 안정적, 포트 필요 없음) ⭐ 추천
```bash
python trial.py <입력파일.xlsx>
```
- 입력 엑셀은 왼쪽 파일탐색기에 **드래그&드롭**해서 올리기 (파일명은 영어 권장, 예: `list.xlsx`)
- 실행하면 ID/PW를 물어봄 (PW는 화면에 안 보임) → 수집 로그가 바로 뜸
- 끝나면 `<입력파일>_결과.xlsx` 생성 → 파일탐색기에서 우클릭 → **Download**
- 모의 모드로 먼저 시험: `python trial.py --mock`

## 2. 방법 B — 웹 UI
```bash
bash run_web.sh
```
- 기존 streamlit 정리 + 새로 실행 + 상태까지 한 번에 보여줌
- `✅ streamlit 정상` 이 뜨면 → 아래쪽 **[포트/PORTS]** 탭 → **8501**의 **🌐 지구본** 클릭
- 404가 나면 → 8501 **우클릭 → Port Visibility → Public** → 다시 지구본
  - 그래도 404면 방법 A(trial.py)를 쓰세요. (streamlit 자체는 정상, GitHub 포트 연결만 가끔 말썽)
- ⚠️ streamlit 켠 터미널엔 다른 명령을 치지 마세요. 명령이 더 필요하면 새 터미널(`+`).

---

## 자주 나는 에러 대처

| 증상 | 해결 |
|------|------|
| `Executable doesn't exist` / `libatk...` | `python -m playwright install --with-deps chromium` |
| 웹 주소 404 | 방법 B의 Public 설정, 안 되면 trial.py 사용 |
| `No module named playwright` (sudo에서) | 정상 — sudo 없이 실행하면 됩니다 |
| 로그인 실패 `나의정보 미노출` | 동시접속 팝업 처리 중. 본인 브라우저에서 나이스비즈라인 로그아웃 후 재시도 |

## 세션/동시접속
- 동시접속 **1명 제한**. 스크립트가 로그인 시 기존 접속을 자동 강제종료함.
- 조회 도는 동안 **본인 브라우저에서는 나이스비즈라인 로그아웃**해두면 충돌이 적음.
- 9분마다 '로그인 연장'을 자동 클릭해 세션 유지.
