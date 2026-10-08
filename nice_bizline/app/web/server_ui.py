"""서버 모드 UI (nice-web): 작업 등록 · 대기열 · 진행률 · 결과 다운로드.

수집은 이 프로세스가 하지 않는다 — nice-worker 가 공유 볼륨의 큐를 보고 실행한다.
그래서 탭을 닫아도, 포털 세션이 끝나도, 재배포돼도 작업은 이어진다.

사용자 식별은 포털 게이트웨이가 넣어 주는 X-Auth-Request-Email 헤더만 믿는다.
비어 있으면 미인증 요청이므로 통과시키지 않는다. 로컬 개발에서만 DEV_FAKE_EMAIL 로 대체.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import streamlit as st

from nice_bizline.app.excelio.reader import available_filter_fields, read_company_list
from nice_bizline.app.server import jobs as J

PORTAL_URL = "https://portal.bluetns.co.kr/"
PORTAL_LOGOUT_URL = "https://portal.bluetns.co.kr/logout"

_STATUS_LABEL = {
    J.PENDING: "대기",
    J.RUNNING: "실행 중",
    J.DONE: "완료",
    J.STOPPED: "안전 정지(재개 가능)",
    J.CANCELED: "취소됨",
    J.ERROR: "오류",
}


def current_email() -> str:
    try:
        headers = st.context.headers
    except Exception:
        headers = {}
    email = (headers.get("X-Auth-Request-Email") or "").strip()
    if not email:
        # 로컬 개발 전용. 운영 compose 에는 이 변수를 두지 않는다.
        email = os.environ.get("DEV_FAKE_EMAIL", "").strip()
    return email


def _store() -> J.JobStore:
    return J.JobStore(os.environ.get("DATA_DIR", "/data"))


def render(cfg: dict) -> None:
    st.title("나이스비즈라인 기업정보 조회")
    email = current_email()
    if not email:
        st.error("포털을 통해 접속해 주세요. (인증 정보가 없습니다)")
        st.stop()

    store = _store()

    with st.sidebar:
        st.markdown(f"**{email}**")
        st.link_button("포털로 이동", PORTAL_URL)
        st.link_button("로그아웃", PORTAL_LOGOUT_URL)
        st.caption("수집은 서버가 대신 수행합니다. 탭을 닫아도 작업은 계속됩니다.")

    tab_new, tab_jobs = st.tabs(["새 작업 등록", "작업 목록"])
    with tab_new:
        _render_new_job(store, email)
    with tab_jobs:
        _render_job_list(store, email)


# ──────────────────────────────── 새 작업 ────────────────────────────────
def _render_new_job(store: J.JobStore, email: str) -> None:
    st.subheader("① 입력 파일")
    uploaded = st.file_uploader(
        "회사 목록 엑셀 (.xlsx)", type=["xlsx"],
        help="1열은 회사명(고객사/업체명 등 헤더 자동 인식). 사업자번호·대표자명·주소 열이 있으면 정확도 향상.",
    )
    if uploaded is None:
        st.info("엑셀을 올리면 인식된 컬럼과 필터 옵션이 나타납니다.")
        return

    tmp = Path(tempfile.gettempdir()) / f"preview_{uploaded.name}"
    tmp.write_bytes(uploaded.getbuffer())
    try:
        companies = read_company_list(str(tmp))
    except Exception as e:
        st.error(f"엑셀 읽기 실패: {e}")
        return
    if not companies:
        st.error("입력이 비어 있습니다.")
        return

    cols = list(companies[0].keys())
    st.success(f"입력 로드 완료 - {len(companies)}건 (컬럼: {cols})")
    with st.expander("첫 5건 미리보기 (프로그램이 인식한 컬럼 그대로)"):
        seen: list[str] = []
        for c in companies[:5]:
            for k in c:
                if k not in seen:
                    seen.append(k)
        st.dataframe([{k: c.get(k, "") for k in seen} for c in companies[:5]])

    # 중복 필터
    st.subheader("② 중복 필터")
    narrow_fields: list[str] = []
    avail = available_filter_fields(companies)
    if avail:
        st.caption("동명 회사가 많을 때 아래 체크한 컬럼이 일치하는 회사만 남깁니다.")
        for f in avail:
            label = {"대표자명": "대표자명 일치", "주소": "주소(지역) 일치"}.get(f, f)
            if st.checkbox(label, value=True, key=f"srv_narrow_{f}"):
                narrow_fields.append(f)
    else:
        st.caption("이 파일에는 중복 필터에 쓸 컬럼(대표자명/주소)이 없어 동명 회사는 모두 수집됩니다.")

    # 결과 필터
    st.subheader("③ 결과 필터 (선택)")
    c1, c2 = st.columns(2)
    with c1:
        use_emp = st.checkbox("종업원수 조건", value=False, key="srv_use_emp")
        min_emp = st.number_input("최소 종업원수(명)", min_value=1, value=20,
                                  disabled=not use_emp, key="srv_min_emp")
    with c2:
        use_sales = st.checkbox("매출액 조건", value=False, key="srv_use_sales")
        min_sales = st.number_input("최소 매출액(억원)", min_value=1, value=10,
                                    disabled=not use_sales, key="srv_min_sales")
    mode = "AND"
    if use_emp and use_sales:
        pick = st.radio("두 조건 결합 방식",
                        ["AND - 둘 다 충족해야 수집", "OR - 하나만 충족해도 수집"],
                        horizontal=True, key="srv_mode")
        mode = "OR" if pick.startswith("OR") else "AND"
    result_filter = None
    if use_emp or use_sales:
        result_filter = {"mode": mode}
        if use_emp:
            result_filter["min_employees"] = int(min_emp)
        if use_sales:
            result_filter["min_sales"] = int(min_sales) * 100

    # 등록
    st.subheader("④ 등록")
    pending = [j for j in store.list() if j["status"] in (J.PENDING, J.RUNNING)]
    est_min = sum(_remaining(j) for j in pending) * 0.5          # 건당 ~30초
    if pending:
        st.caption(f"현재 대기·실행 중 {len(pending)}건 — 이 작업은 약 {int(est_min)}분 뒤 시작 예상 "
                   f"(이 작업 자체는 {len(companies)}건 × 20~30초 ≈ {len(companies) // 2}~{len(companies) // 2 + len(companies) // 4}분)")
    if st.button("▶ 작업 등록", type="primary"):
        job = store.create(
            name=uploaded.name, input_bytes=bytes(uploaded.getbuffer()),
            owner_email=email,
            options={"narrow_fields": narrow_fields or None, "result_filter": result_filter},
        )
        st.success(f"작업 등록 완료 ({job['id']}). '작업 목록' 탭에서 진행 상황을 확인하세요. "
                   "이 탭을 닫아도 작업은 계속됩니다.")


def _remaining(job: dict) -> int:
    p = job.get("progress") or {}
    total, cur = int(p.get("total") or 0), int(p.get("current") or 0)
    return max(0, total - cur) if total else 0


# ──────────────────────────────── 작업 목록 ────────────────────────────────
@st.fragment(run_every="5s")
def _render_job_list(store: J.JobStore, email: str) -> None:
    jobs = store.list()
    if not jobs:
        st.info("등록된 작업이 없습니다.")
        return

    pending_ids = [j["id"] for j in jobs if j["status"] == J.PENDING]
    running = any(j["status"] == J.RUNNING for j in jobs)
    st.caption(f"대기 {len(pending_ids)}건 · 실행 중 {1 if running else 0}건 · "
               f"5초마다 자동 갱신 · 동시 실행 1 (NICE 계정 동시접속 제한)")

    for job in reversed(jobs):          # 최신 작업이 위로
        mine = job.get("owner_email") == email
        status = job["status"]
        label = _STATUS_LABEL.get(status, status)
        if status == J.PENDING:
            pos = pending_ids.index(job["id"]) + 1 + (1 if running else 0)
            label += f" · 앞에 {pos - 1}건"
        head = f"{'🟢' if status == J.RUNNING else '•'} {job['name']} — {label}"
        with st.expander(f"{head}  ({job.get('created_at', '')} · {job.get('owner_email', '')})",
                         expanded=(status == J.RUNNING)):
            p = job.get("progress") or {}
            if status == J.RUNNING and p.get("total"):
                pct = p["current"] / max(1, p["total"])
                st.progress(pct, text=f"{p['current']}/{p['total']} ({int(pct * 100)}%)  {p.get('name', '')}")
            if job.get("counts"):
                c = job["counts"]
                cols = st.columns(6)
                cols[0].metric("전체", c.get("total", 0))
                cols[1].metric("성공", c.get("success", 0))
                cols[2].metric("미발견", c.get("not_found", 0))
                cols[3].metric("확인필요", c.get("ambiguous", 0))
                cols[4].metric("오류", c.get("error", 0))
                cols[5].metric("필터 제외", c.get("필터제외", 0))
            if job.get("error"):
                st.error(job["error"])

            if mine:
                b1, b2, _ = st.columns([1, 1, 3])
                if status in (J.PENDING, J.RUNNING):
                    if b1.button("취소", key=f"cancel_{job['id']}"):
                        store.request_cancel(job["id"], email)
                        st.rerun(scope="fragment")
                res = store.result_path(job["id"])
                if status in (J.DONE, J.STOPPED, J.CANCELED) and os.path.exists(res):
                    with open(res, "rb") as f:
                        data = f.read()
                    b2.download_button(
                        "📥 결과 다운로드", data=data,
                        file_name=f"{Path(job['name']).stem}_나이스비즈라인결과.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"dl_{job['id']}",
                        on_click=lambda jid=job["id"]: store.record_download(jid, email),
                    )
                tail = store.tail_log(job["id"], 40)
                if tail:
                    with st.container(height=220):
                        st.code(tail, language=None)
            else:
                st.caption("다른 사람의 작업입니다 — 결과·취소는 올린 사람만 가능합니다.")
