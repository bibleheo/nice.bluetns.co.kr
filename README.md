# 나이스비즈라인 기업정보 조회 자동화

엑셀에 정리된 회사 목록을 받아 나이스비즈라인(NICE비즈인포)에서 기본정보·재무정보를 자동 수집하고 결과 엑셀로 출력하는 프로그램.

- **웹 UI** (Streamlit) — 브라우저에서 실행, GitHub Codespaces 지원
- **데스크톱 앱** (Tkinter) — Windows .exe 배포 가능
- 공통 수집 파이프라인 (Playwright 기반)

자세한 문서는 [`nice_bizline/README.md`](nice_bizline/README.md), 셀렉터 채우는 법은 [`nice_bizline/SELECTORS_GUIDE.md`](nice_bizline/SELECTORS_GUIDE.md) 참고.

---

## 빠른 시작 (3가지 방법)

### 방법 A: GitHub Codespaces (설치 불필요, 가장 쉬움)

1. 이 저장소 페이지 → 초록색 **`< > Code`** → **Codespaces** 탭 → **Create codespace**
2. 2분 대기 (의존성 자동 설치)
3. 터미널에서:
   ```bash
   streamlit run nice_bizline/app/web/streamlit_app.py
   ```
4. 브라우저 새 탭에서 웹 앱 사용 (모의 모드로 먼저 테스트)

### 방법 B: 로컬 웹 UI

Python 3.11 권장 (3.13+ 는 일부 패키지 미지원 가능):

```bash
python -m pip install -r requirements.txt
streamlit run nice_bizline/app/web/streamlit_app.py
```

→ 브라우저에 http://localhost:8501 자동 열림

**Windows 사용자**: 명령어 입력 없이 저장소 루트의 **`웹앱_실행.bat`** 파일을
더블클릭하면 패키지 설치와 웹앱 실행이 자동으로 진행됩니다. (Python 3.11 설치 필요)

### 방법 C: 데스크톱 앱

```bash
python -m pip install -r requirements.txt
python -m nice_bizline           # 실제 모드
python -m nice_bizline --mock    # 모의 모드
```

---

## 실제 사이트 연동 전 필수 작업

1. **사내 법무/이용약관 검토**
2. **DOM 셀렉터 채우기** — `nice_bizline/config.yaml`의 placeholder를 실제 페이지에서 추출 ([가이드](nice_bizline/SELECTORS_GUIDE.md))
3. **재무 단위 확인** 후 시범 5건 실행

## 테스트

```bash
python -m pip install pytest
python -m pytest nice_bizline/tests/ -v
```

66건 단위·통합 테스트 (사이트 접근 없이 실행).

## 구조

```
.
├─ nice_bizline/           # 앱 패키지
│  ├─ app/
│  │  ├─ core/             # pipeline, collector, matcher, ...
│  │  ├─ ui/               # Tkinter 데스크톱
│  │  ├─ web/              # Streamlit 웹
│  │  └─ excelio/          # 엑셀 읽기/쓰기
│  ├─ tests/
│  ├─ config.yaml          # 셀렉터/타이밍/매칭 설정
│  ├─ README.md
│  └─ SELECTORS_GUIDE.md
├─ .devcontainer/          # Codespaces 설정
├─ .github/workflows/      # Windows .exe 자동 빌드
├─ nice_bizline.spec       # PyInstaller
└─ requirements.txt
```
