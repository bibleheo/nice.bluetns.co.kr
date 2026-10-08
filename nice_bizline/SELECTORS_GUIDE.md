# DOM 셀렉터 채우기 가이드

`config.yaml`의 `selectors.*` 값은 실제 사이트에 접속하지 않고는 알 수 없어 placeholder로 남겨두었습니다. 이 문서는 실제 페이지를 보며 그 값을 채우는 방법을 안내합니다.

> **사전 조건**: 사내 법무 검토 + 약관 확인 완료. 본인 계정으로 정상 접근 가능.

---

## 1. Playwright codegen으로 빠르게 추출 (권장)

가장 빠른 방법. 브라우저를 직접 조작하면서 Playwright가 셀렉터를 자동 생성합니다.

```bash
python -m playwright install chromium
python -m playwright codegen https://www.nicebizinfo.com
```

새 창에서:

1. 로그인 → 검색창에 회사명 입력 → 검색 버튼 클릭 → 결과 행 클릭 → 상세 페이지 진입
2. 좌측에 생성되는 코드(예: `page.locator("input[name='userId']")`)를 그대로 `config.yaml`에 옮김
3. 같은 의미의 더 안정적인 셀렉터가 보이면 우선: `id` > `name` > `data-*` > 클래스 > 구조 기반

---

## 2. 수동으로 추출 (F12 개발자도구)

### 2-1. 로그인 페이지

`selectors.login.*` 항목:

| 키 | 찾는 법 |
|----|---------|
| `id_input` | 아이디 입력 칸 → 우클릭 "검사" → `<input>` 요소의 가장 식별력 있는 속성 (보통 `name`) |
| `pw_input` | 비밀번호 입력 칸도 동일 |
| `submit_btn` | "로그인" 버튼. `<button type="submit">` 또는 `<a>` 형태일 수 있음 |
| `logged_in_marker` | 로그인 직후 헤더에 노출되는 사용자명/마이페이지 링크 등. **로그인 성공 판정 기준**이므로 비로그인 시에는 절대 보이지 않아야 함 |

### 2-2. 검색 결과 페이지

회사명으로 검색 후 결과 목록이 나오는 화면에서:

| 키 | 찾는 법 |
|----|---------|
| `query_input` | 회사명 입력 칸 |
| `submit_btn` | 검색 버튼 |
| `result_rows` | 결과 1건당 1행. 보통 `<tr>` 또는 `<li>`/`<div class="row">`. **부모 컨테이너 안의 반복 요소** 셀렉터 |
| `result_company_name` | 행 안에서 회사명 노드 (`result_rows` 기준 상대 셀렉터) |
| `result_biz_no` | 행 안에서 사업자번호 노드 |
| `result_ceo` | 행 안에서 대표자명 노드 |
| `result_address` | 행 안에서 주소 노드 |
| `result_link` | 상세 페이지로 이동하는 `<a>` 태그. `href` 속성에서 URL 추출 |
| `next_page_btn` | "다음" 페이지 버튼. **비활성 상태일 때 매칭되지 않는 셀렉터** 권장 (예: `a.btn-next:not(.disabled)`). 빈 문자열로 두면 1페이지만 수집 |

> **페이지네이션 한도**: `timing.max_search_pages` (기본 3)로 안전 한도 설정. 동명이인이 많은 회사를 검색해도 무한 루프 방지. 1로 두면 페이지네이션 비활성.

> **팁**: 행 셀렉터 검증은 콘솔에서 `document.querySelectorAll("표.result-list tr").length` 같은 식으로 후보 수를 확인하면 빠릅니다.

### 2-3. 상세 페이지

기업 한 곳을 클릭해서 들어간 화면에서 각 항목의 값을 표시하는 요소를 찾습니다:

| 키 | 항목 |
|----|------|
| `company_name` | 회사명 (보통 페이지 상단 큰 글씨) |
| `ceo` | 대표자명 |
| `biz_no` | 사업자등록번호 |
| `address` | 본사 주소 |
| `industry` | 업종/표준산업분류 |
| `founded` | 설립일 |
| `phone` | 대표 전화번호 |
| `employees` | 종업원수 |
| `sales` | 매출액 (최근 결산) |
| `operating_profit` | 영업이익 |
| `net_income` | 당기순이익 |
| `credit_rating` | 신용등급 (계정 등급에 따라 노출 안 될 수 있음) |

> **팁**: 같은 라벨이 페이지 여러 곳에 있을 수 있습니다(요약 박스 vs 상세 탭). 더 안정적인 쪽을 선택. 가능하면 `data-field="sales"` 같은 의미적 속성을 우선.

### 2-4. 로그인 페이지 감지 (세션 만료)

`selectors.login_page_markers`는 세션 만료로 로그인 화면으로 튕긴 경우 감지에 사용:

- `url_contains`: 로그인 페이지 URL에 항상 포함되는 부분 (예: `/login`, `/auth`)
- `dom_marker`: 로그인 페이지에만 있는 DOM 요소 (보통 `id_input`과 동일하게 두면 충분)

---

## 3. 채운 뒤 검증 절차

1. 작은 입력 파일로 시범 실행 (5건 정도):
   ```bash
   python -m nice_bizline.main
   ```
2. 로그 확인:
   - "로그인 성공" 메시지가 뜨는가? (안 뜨면 `logged_in_marker` 잘못됨)
   - 검색 결과 0건/다수 비율이 비정상적이지 않은가?
   - 상세 파싱에서 모든 필드가 채워지는가? (빈 값이 많으면 셀렉터가 잘못됐거나 권한 부족)
3. 결과 엑셀의 `결과` 시트에서 컬럼별 값을 육안 검증

---

## 4. 자주 발생하는 함정

- **셀렉터가 너무 구체적**: 클래스 이름이 빌드마다 바뀌는 사이트 (CSS-in-JS). `data-*`/구조 기반으로 대체.
- **요소가 동적으로 로드**: 검색 버튼을 눌렀는데 결과가 즉시 안 보이고 AJAX로 채워지는 경우. `result_rows` 대기 타임아웃을 키우거나 명시적 `wait_for_selector` 추가.
- **모달/팝업 차단**: 첫 로그인 시 동의 팝업 등이 떠서 후속 클릭이 막힘. `collector.login()`에 팝업 닫기 로직 추가 필요.
- **권한 미제공 항목**: 신용등급/재무가 빈 값 → 정상. 워커가 자동으로 비고에 "권한없음/미제공" 기록.

---

## 5. 단위 변환 확인 (재무 항목)

채운 뒤 첫 실행 결과를 보고 `worker.py:_finance_columns()`의 단위 변환 규칙을 조정:

- 사이트가 매출액을 "원 단위"로 표기 → `백만원` 컬럼 헤더와 맞추려면 `÷ 1_000_000`
- 사이트가 이미 "백만원 단위"로 표기 → 변환 불필요 (현재 기본값)
- 사이트가 "천원 단위" → `÷ 1_000`

확인 방법: 알려진 회사(예: 삼성전자)의 매출액을 사이트와 결과 엑셀에서 비교.

---

## 6. 실사이트(nicebizline.com) 반영 현황 — 2026-07

실제 사이트를 확인해 셀렉터를 채운 상태입니다. **사이트는 `www.nicebizline.com`
(Vue/Vuetify SPA)** 이며, 다음이 반영됨:

| 구분 | 값/방식 |
|------|---------|
| 로그인 | `/cm/lgn`, id=`input[type=text]`, pw=`input[type=password]`, 버튼=`button:has-text('로그인')`, 성공판정=`img[alt='나의정보']` |
| 검색 | `#search` 입력 → `img[alt='검색하기']` 클릭. 결과는 `v-data-table` |
| 결과 필드 | td 클래스가 무의미 → **열 위치(nth-child)**로 지정 (2 기업명/3 사업자번호/4 대표자/5 소재지) |
| 상세 진입 | **href 없음** → 행의 '개요' 버튼 클릭 시 **같은 페이지 인라인 렌더** |
| 상세 파싱 | 기본정보 `th(라벨)→td(값)`, 재무 KPI 카드 `.section__info__label/value` |
| 재무 단위 | **억원/만원 혼재** → `normalizer.amount_to_millions()`로 백만원 통일. 최신 결산 1개년만 기록 |
| 노이즈 | 사업자번호 없는 펀드/ETF 행 제외 |
| 동명 회사 | 사업자번호 미입력 시 상호 정확 일치 회사를 **전부 수집(여러 행)** |

### ⚠️ 실행으로 검증 필요한 동적 동작
코드는 제공된 실제 DOM 기준으로 작성했으나, 아래는 **로그인 후 시범 실행**으로만 확정됩니다.

1. **검색 URL 토큰** — `site.search_url`의 `/workspace/FD100/z6pi` 끝부분이 세션마다
   바뀌면 접근 실패 가능. 이를 대비해 collector는 *현재 페이지에 `#search`가 있으면
   그대로 사용하고, 없을 때만 search_url→base_url 순으로 이동*하도록 되어 있음.
2. **검색 실행** — 아이콘 클릭으로 결과가 뜨는지(안 되면 Enter 폴백).
3. **개요 클릭 후 렌더** — `.kpi__info`(재무 카드)가 나타날 때까지 대기하도록 구현됨.

### 시범 실행 방법 (헤드리스 끄고 눈으로 확인 권장)

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

`config.yaml`에서 `browser.headless: false`로 바꾼 뒤, 실제 ID/PW로 5건 정도
소량 실행 → 로그에서 막히는 지점을 확인하고 위 1~3을 조정.
