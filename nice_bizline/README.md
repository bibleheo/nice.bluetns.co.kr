# 나이스비즈라인 기업정보 조회 자동화

엑셀에 정리된 회사 목록을 받아 나이스비즈라인(NICE비즈인포)에서 기본정보·재무정보를 자동 수집하고 결과 엑셀로 출력하는 데스크톱 앱.

## 설치

```bash
python -m pip install -r ../requirements.txt
python -m playwright install chromium   # 실제 수집 모드에서만 필요
```

## 실행 방식 3가지

### 방식 A: 웹 UI (GitHub Codespaces에서 브라우저로) — **가장 쉬움**

설치 없이 브라우저에서 바로 사용.

1. GitHub 저장소 페이지 → 상단 초록색 **`< > Code`** 버튼 → **Codespaces** 탭
2. **`Create codespace on claude/nice-bizline-scraper-FK3zL`** 클릭
3. 2분 정도 기다리면 VS Code가 브라우저에서 열리고 의존성 자동 설치
4. 하단 터미널에서:
   ```bash
   streamlit run nice_bizline/app/web/streamlit_app.py
   ```
5. 브라우저 새 탭에서 웹 앱이 자동으로 열림
   - 사이드바에서 "모의 모드" 켠 상태로 시작
   - 엑셀 업로드 → 조회 시작 → 결과 다운로드

### 방식 B: 로컬 웹 UI

본인 PC에 Python이 있으면 아래 한 줄:

```bash
python -m pip install -r requirements.txt
streamlit run nice_bizline/app/web/streamlit_app.py
```

→ 브라우저에 http://localhost:8501 자동 열림

### 방식 C: 데스크톱 앱 (Tkinter)

```bash
python -m nice_bizline           # 실제 모드
python -m nice_bizline --mock    # 모의 모드
```

## 빌드 (Windows .exe 배포)

```bash
python -m pip install pyinstaller
python -m pip install -r ../requirements.txt
python -m playwright install chromium       # 빌드 호스트에 1회

# 프로젝트 루트에서 실행
cd ..
pyinstaller nice_bizline.spec --clean --noconfirm
```

산출물:
- `dist/나이스비즈라인_조회/나이스비즈라인_조회.exe` — 실행 파일
- `dist/나이스비즈라인_조회/_internal/nice_bizline/config.yaml` — 사용자가 셀렉터를 수정하는 설정 파일
- `dist/나이스비즈라인_조회/` 폴더 전체를 zip으로 배포

배포 받은 PC에서는 첫 1회 `python -m playwright install chromium` 또는
사내 공용 브라우저 경로를 `PLAYWRIGHT_BROWSERS_PATH` 환경변수로 지정해야 합니다.

## 입력 엑셀 형식

첫 시트의 1행을 헤더로 인식합니다. 헤더가 없으면 1열을 회사명으로 간주합니다.

| 회사명 (필수) | 사업자번호 (선택) | 대표자명 (선택) | 주소 (선택) |
|--------------|------------------|----------------|------------|
| 삼성전자      | 124-81-00998     | 한종희          | 경기도 수원시 |
| 현대자동차    |                  |                |            |

헤더 이름은 유연하게 인식합니다. `회사명` 대신 `업체명·상호·기업명`,
`주소` 대신 `소재지·사업장주소` 등을 써도 됩니다.

사업자번호/대표자명/주소가 있으면 동명 회사 매칭 정확도가 크게 올라갑니다.
특히 지점이 많은 명단(예: 지하안전평가전문기관)은 `주소` 컬럼이 있으면
같은 이름의 다른 지점을 구분하는 데 도움이 됩니다.

같은 `회사명|사업자번호` 조합이 입력에 중복으로 들어오면 첫 건만 처리하고
나머지는 로그에 "중복 입력 - 스킵"으로 안내합니다.

## 테스트

```bash
python -m pip install pytest
python -m pytest nice_bizline/tests/ -v
```

핵심 비즈니스 로직(matcher / normalizer / session / credentials / excelio /
worker 파이프라인)에 대해 46개 단위·통합 테스트 포함. 사이트 접근 없이
실행됩니다.

## 결과 엑셀

`<입력파일명>_나이스비즈라인결과_YYYYMMDD_HHMM.xlsx`로 저장되며 4개 시트가 생성됩니다:

- **결과**: 회사별 1행 (회사명/대표자/사업자번호/주소/업종/설립일/대표번호/종업원수/매출액/영업이익/당기순이익/신용등급/조회상태/조회일시/비고)
- **미발견·오류**: 검색 0건이거나 오류로 처리된 건
- **확인필요**: 동명 후보가 다수이고 점수가 임계치 미달인 건 (수동 검수용)
- **실행로그**: 시작·종료 시각, 총건수/성공/미발견/확인필요/오류 집계

## 구조

```
nice_bizline/
├─ main.py                     # 앱 진입점
├─ config.yaml                 # 사이트 URL, DOM 셀렉터, 타이밍, 매칭 가중치
├─ app/
│   ├─ ui/main_window.py       # Tkinter UI (UI 스레드)
│   ├─ core/
│   │   ├─ worker.py           # 백그라운드 파이프라인 (수집 스레드)
│   │   ├─ collector.py        # NiceBizlineCollector + MockCollector (교체 지점)
│   │   ├─ session.py          # 9분 선제 재로그인 타이머
│   │   ├─ matcher.py          # 동명이인 점수 매칭
│   │   └─ normalizer.py       # 금액/날짜/사업자번호 정규화
│   └─ excelio/
│       ├─ reader.py
│       └─ writer.py
└─ logs/
```

## 개발/배포 전 확인 필요 (명세 11장 미확정 항목)

- [ ] **실제 사이트 DOM 셀렉터** — `config.yaml`의 `selectors.*`에 들어있는 placeholder를 실제 페이지 확인 후 채워야 합니다. 채우는 방법은 [`SELECTORS_GUIDE.md`](./SELECTORS_GUIDE.md) 참고. 모의 모드(`--mock`)에서는 채울 필요 없음.
- [ ] **재무 단위** — 사이트에서 매출액 등이 "원" 단위인지 "백만원" 단위인지 확인 후 `worker.py:_finance_columns()` 변환 로직 조정 필요.
- [x] **재무 3개년 옵션 구현** — UI 옵션에서 3개년 선택 시 collector가 `config.yaml`의 `selectors.detail.financial_table.*` 셀렉터로 다년 재무표를 파싱하고, 결과 엑셀이 `매출액(2025)/매출액(2024)/매출액(2023)` 형태로 연도별 컬럼을 동적 생성. 실제 사이트의 다년 재무표 셀렉터는 [`SELECTORS_GUIDE.md`](./SELECTORS_GUIDE.md) §2-3 참고.
- [x] **계정 저장** — `keyring`으로 OS 자격증명 저장소 연동 완료. "저장" 체크 시 Windows 자격 증명 관리자 / macOS Keychain / Linux Secret Service에 ID/PW 저장 (평문 파일 저장 없음). 백엔드 미존재 환경에서는 자동으로 비활성화되어 매 실행 시 재입력 모드로 동작.
- [x] **PyInstaller 패키징** — `nice_bizline.spec` 추가. 위 "빌드" 섹션 참고. (Windows 환경에서 빌드 권장)
- [ ] **이용약관/저작권 검토** — 사내 법무 검토 권장. 공식 API/제휴 가능 여부 확인 권장. `collector.py`의 `BaseCollector` 인터페이스만 유지하면 공식 API로 최소 변경 교체 가능.

## 로그 파일

각 실행마다 입력 엑셀과 짝지어 로그 파일이 생성됩니다.

- 위치: 입력 파일과 같은 폴더
- 파일명: `<입력파일명>_나이스비즈라인로그_YYYYMMDD_HHMM.log`
- 내용: UI에 표시되는 모든 로그(레벨 포함)가 timestamp와 함께 기록
- 사후 디버깅, 보고용으로 활용
- UI 하단 "로그 파일 열기" 버튼으로 즉시 확인 가능

## 체크포인트 / 재개

명세 9장 "주기적 임시저장 권장" + FR-8 재개 옵션.

- 수집 중 **매 10건마다** 입력 엑셀 옆에 `<입력파일명>.progress.json`을 원자적으로 저장 (전체 결과 + 처리한 회사 키 + 옵션)
- 중단·강제종료 시에도 마지막 저장본까지 보존
- 다음 실행 시 같은 입력 파일을 선택하면 다이얼로그 표시:
  - **예** = 이어서 진행 (이미 처리한 회사 자동 스킵)
  - **아니오** = 처음부터 새로 시작 (체크포인트 삭제)
  - **취소** = 시작하지 않음
- 정상 완료(중단되지 않음) 시 결과 엑셀 저장 후 체크포인트 자동 정리
- 멱등 키는 `회사명|사업자번호` 조합

## 세션 관리

로그인 후 10분 절대시간으로 만료되는 사이트 특성에 맞춰:

1. 매 회사 처리 직전 경과 시간 확인 → 9분 초과 시 선제 재로그인
2. 응답 페이지가 로그인 화면이면(`LoginRequired`) 재로그인 후 해당 건 1회 재시도
3. 2회 연속 실패 시 상태 `오류`로 기록하고 다음 건 진행

## 검색 페이지네이션

동명이인 회사가 많을 경우 검색 결과가 여러 페이지에 걸쳐 표시될 수 있어 누락을 방지합니다.

- `config.yaml`의 `selectors.search.next_page_btn`에 "다음 페이지" 버튼 셀렉터 지정
- `timing.max_search_pages` (기본 3)로 안전 한도
- 1페이지만 보려면 `max_search_pages: 1`로 설정 (또는 next_page_btn 빈 문자열)
- 비활성 상태일 때 매칭되지 않는 셀렉터 권장 (예: `a.btn-next:not(.disabled)`)

## 안정성

- 단일 회사 오류는 전체 작업을 멈추지 않음 (해당 건만 기록 후 계속 진행)
- 사용자 중단 시 현재 회사 처리 완료 후 안전 정지, 처리분까지 엑셀 저장
- 요청 사이 1-3초 랜덤 지연, 병렬 호출 금지
