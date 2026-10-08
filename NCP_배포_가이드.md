# NCP 서버 배포 가이드 (GitHub Actions → SSH → docker compose)

포털 편입 브리프(2026-10-08) 6절 방식입니다. 포털은 NICE 를 빌드하지 않고, **NICE 저장소의
GitHub Actions 가 NCP 서버에 SSH 로 들어가 `docker compose up -d --build`** 를 실행합니다.

```
GitHub (push main / 수동 실행)
   └─ Actions: rsync 소스 → 서버 .env 기록 → docker compose up → 헬스체크
         └─ NCP 서버 /srv/nice.bluetns.co.kr
               ├─ nice-web     (Streamlit, portal-net)  ← 게이트웨이가 nice-web:8501 로 연결
               ├─ nice-worker  (Playwright·Chromium, 수집 전담)
               └─ 볼륨 nice-data (작업 큐 · 업로드 · 체크포인트 · 결과)
```

---

## 0. 구성 파일 (저장소에 있음)

| 파일 | 역할 |
|------|------|
| `Dockerfile` | web/worker 공용 이미지 (Python 3.12 + Playwright Chromium) |
| `docker-compose.yml` | nice-web + nice-worker, 공유 볼륨, portal-net, 자원 상한 |
| `.github/workflows/deploy.yml` | 배포 워크플로 |
| `.env.example` | 서버 `.env` 형식 (실제 값은 Secrets → 서버에만) |
| `nice_bizline/app/server/` | 작업 큐(jobs.py) · worker 데몬(worker.py) |
| `nice_bizline/app/web/server_ui.py` | 서버 모드 화면 (작업 등록·목록·다운로드) |

---

## 1. 한 번만 하는 준비

### 1-1. 배포 키 만들기 (내 PC에서)
```bash
ssh-keygen -t ed25519 -C "nice-deploy@github-actions" -f nice_deploy
# 비밀번호는 Enter 두 번(빈 값)
```
- `nice_deploy.pub` **한 줄** → 포털 팀에 전달 (회신 13번)
- `nice_deploy` (개인키) → GitHub Secrets `DEPLOY_SSH_KEY` 에만. **서버에서 키를 만들지 않는다.**

### 1-2. 포털 팀에게 받을 것
- 배포 계정 이름(예: `nice`), 디렉터리(`/srv/nice.bluetns.co.kr`)
- 서버 주소(IP/호스트명)
- **호스트 키 한 줄** (`ssh-keyscan -t ed25519 <서버>` 결과) → Secrets `DEPLOY_KNOWN_HOSTS`

### 1-3. GitHub Secrets 등록
저장소 → Settings → Secrets and variables → Actions → New repository secret

| Secret | 값 |
|--------|----|
| `DEPLOY_SSH_KEY` | 1-1 개인키 파일 내용 전체 |
| `DEPLOY_KNOWN_HOSTS` | 1-2 호스트 키 한 줄 |
| `DEPLOY_HOST` | 서버 주소 |
| `DEPLOY_USER` | 배포 계정 (예: `nice`) |
| `NICE_ID` | 나이스비즈라인 서버 전용 계정 ID |
| `NICE_PW` | 나이스비즈라인 계정 비밀번호 |

선택 Variable: `NCP_DEPLOY_DIR` (기본 `/srv/nice.bluetns.co.kr`)

### 1-4. 서버 쪽 전제 (포털 팀 확인)
- 배포 계정이 `docker` 그룹
- 외부 네트워크 `bluetns-portal_portal-net` 이 존재 (포털 compose 가 만듦)
- 서버 → NICE BizLINE 사이트로 아웃바운드 가능 (IP 제한 여부는 회신 4번)

---

## 2. 배포

### 첫 배포 (수동 실행)
1. GitHub → **Actions** → **Deploy to NCP** → **Run workflow**
2. 브랜치 선택 (`main` 병합 전이면 `claude/nice-bizlin-app-t82c1b`) → Run
3. 로그에서 `nice-web health: healthy` 확인

### 이후 배포
- `main` 에 push 하면 자동 배포
- 또는 Actions 에서 수동 실행

### 배포 후 포털 쪽 작업 (포털 팀)
- 카탈로그 등록: `nice.bluetns.co.kr` → upstream `nice-web:8501`, healthPath `/_stcore/health`
- Entra 그룹 `app-nice` 에 시험자 추가
- ※ 카탈로그 등록은 **앱이 떠 있는 뒤에** (없으면 배포 검사 실패)

---

## 3. 운영 중 자주 쓰는 명령 (서버에서, 배포 계정으로)

```bash
cd /srv/nice.bluetns.co.kr
docker compose ps                         # 상태
docker compose logs -f --tail=100 nice-worker   # 수집 로그
docker compose logs -f --tail=100 nice-web
docker compose restart nice-worker        # worker 재시작 (실행 중 작업은 체크포인트부터 자동 재개)
docker compose up -d --build              # 수동 재배포
```

작업 데이터 위치: 볼륨 `nice-data` → `/data/jobs/<작업id>/` (input.xlsx · result.xlsx · log.txt · job.json)
```bash
docker compose exec nice-worker ls -la /data/jobs
docker compose exec nice-worker cat /data/jobs/<작업id>/job.json
```

멈춘 작업 수동 정리: 해당 디렉터리의 `job.json` 에서 `"status"` 를 `"canceled"` 로 바꾸거나 디렉터리 삭제.

---

## 4. 동작 방식 요약

- **직원**: 포털 카드 → 엑셀 업로드 → **작업 등록** → 탭 닫아도 됨 → 나중에 **작업 목록**에서 결과 다운로드
- **nice-web**: 수집하지 않음. 작업을 `/data/jobs` 에 등록하고 진행률·결과만 보여줌. 사용자는 `X-Auth-Request-Email` 헤더로 식별, 없으면 차단
- **nice-worker**: 큐를 5초마다 확인 → 가장 오래된 작업부터 **한 번에 하나** 실행 → 10건마다 체크포인트 → 결과 xlsx 저장
  - 재배포/재시작 시 `running` 상태였던 작업을 자동으로 pending 으로 되돌려 **멈춘 지점부터 재개**
  - 끊김(중복 로그인·네트워크) 시 15초~5분 백오프로 자동 재로그인
  - 하루 한 번 보관 기간(30일/debug 7일) 지난 파일 삭제
- **취소**: 본인 작업만. 대기 중이면 즉시, 실행 중이면 현재 회사까지 처리 후 안전 정지(처리분 저장)

---

## 5. 시험 체크리스트 (포털 브리프 9절)

- [ ] 포털 카드 → NICE 화면, 사이드바에 내 이메일 표시
- [ ] `app-nice` 에 없는 계정 → 포털에서 차단
- [ ] 엑셀 업로드 → 작업 등록 → **탭 닫고** 다시 들어와도 진행 중
- [ ] 작업 도중 **재배포**(Actions 재실행) → 끊긴 지점부터 이어서 진행
- [ ] 두 사람이 동시에 등록 → 하나는 대기, "앞에 N건" 표시
- [ ] 다른 사람 작업은 다운로드·취소 버튼 없음
- [ ] 15분 이상 화면 방치해도 끊기지 않음
- [ ] `/data/debug/` 스크린샷·로그에 비밀번호 없음
- [ ] "포털로 이동" · "로그아웃" 링크 동작

---

## 6. 로컬에서 서버 모드 미리 보기 (선택)

```bash
cp .env.example .env            # NICE_ID/PW 채우기 (또는 MOCK_MODE 로 시험)
docker compose -f docker-compose.yml up --build
# portal-net 이 없는 로컬에서는 networks.portal-net 를 잠시 주석 처리하고,
# nice-web 에 ports: ["8501:8501"], environment DEV_FAKE_EMAIL=me@test 를 임시로 추가.
# (운영 compose 에는 절대 넣지 않는다)
```
