"""서버 모드 UI (nice-web): 작업 목록 · 새 작업 등록 — 디자인 시안 v1 반영.

수집은 이 프로세스가 하지 않는다. nice-worker 가 공유 볼륨의 큐를 보고 실행한다.
그래서 탭을 닫아도, 포털 세션이 끝나도, 재배포돼도 작업은 이어진다.

사용자 식별은 포털 게이트웨이가 넣어 주는 X-Auth-Request-Email 헤더만 믿는다.
비어 있으면 미인증 요청이므로 통과시키지 않는다. 로컬 개발에서만 DEV_FAKE_EMAIL 로 대체.

화면 규칙·문구의 기준: 디자인팀 '상태 규칙 · 화면 문구 · 테마 값' 문서.
주의: Streamlit 마크다운에서 한 줄에 "~" 가 두 번 나오면 취소선이 된다. 범위는 "∼"(U+223C)로 쓴다.
"""
from __future__ import annotations

import datetime as dt
import io
import os
import re
import tempfile
from pathlib import Path

import streamlit as st

from nice_bizline.app.core.timeutil import now_seoul
from nice_bizline.app.excelio.reader import (available_filter_fields, inspect_columns,
                                             read_company_list)
from nice_bizline.app.server import jobs as J

PORTAL_URL = "https://portal.bluetns.co.kr/"
PORTAL_LOGOUT_URL = "https://portal.bluetns.co.kr/logout"

SEC_PER_COMPANY = 25            # 회사 1건당 20∼30초의 중간값
RETENTION_DAYS = 30
TAB_JOBS, TAB_NEW = "작업 목록", "새 작업 등록"

# ── 상태 규칙표 ──────────────────────────────────────────────────────
STATUS = {
    J.PENDING: dict(label="대기", color="gray", icon=":material/schedule:"),
    J.RUNNING: dict(label="실행 중", color="blue", icon=":material/sync:"),
    J.DONE: dict(label="완료", color="green", icon=":material/check_circle:"),
    J.STOPPED: dict(label="안전 정지 · 재개 가능", color="orange", icon=":material/pause_circle:"),
    J.CANCELED: dict(label="취소됨", color="gray", icon=":material/cancel:"),
    J.ERROR: dict(label="오류 · 시작 못 함", color="red", icon=":material/error:"),
}
GROUPS = [
    ("확인이 필요한 작업", (J.STOPPED, J.ERROR)),
    ("진행 중", (J.RUNNING, J.PENDING)),
    ("끝난 작업", (J.DONE, J.CANCELED)),
]
# worker.py 가 남기는 error 값 기준
INPUT_ERRORS = ("입력 읽기 실패", "입력이 비어 있음")
ADMIN_ERRORS = ("서버 계정 미설정",)

FIELD_USE = {
    "회사명": "검색에 씁니다",
    "사업자번호": "같은 회사인지 확인하는 데 씁니다",
    "대표자명": "동명 회사 구분에 씁니다",
    "주소": "동명 회사 구분에 씁니다",
}
OPTIONAL_FIELDS = ("대표자명", "주소", "사업자번호")


# ──────────────────────────────── 공통 ────────────────────────────────
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


def _user_id(email: str) -> str:
    return (email or "").split("@")[0]


# ── 숫자·시간 ──
def _total(job: dict) -> int:
    """입력 회사 수. 예전 작업은 input_total 이 없어 집계·진행값으로 대신한다."""
    return int(job.get("input_total") or (job.get("counts") or {}).get("total")
               or (job.get("progress") or {}).get("total") or 0)


def _done(job: dict) -> int:
    """지금까지 처리한 회사 수."""
    s, c, p = job["status"], job.get("counts") or {}, job.get("progress") or {}
    if s == J.RUNNING:
        return int(p.get("current") or 0)
    if s == J.DONE:
        return _total(job)
    if "processed" in c:
        return int(c["processed"])
    return int(p.get("current") or 0)


def _remaining_sec(job: dict) -> int:
    return max(0, _total(job) - _done(job)) * SEC_PER_COMPANY


def _human(sec: int) -> str:
    m = int(sec) // 60
    if m < 60:
        return f"약 {max(1, m)}분"
    h = m // 60
    if h < 24:
        return f"약 {h}시간"
    return f"약 {max(1, round(h / 24))}일"


def _day_word(t: dt.datetime) -> str:
    return "오늘" if t.date() == now_seoul().date() else f"{t.month}월 {t.day}일"


def _clock(sec_from_now: int, with_day: bool = True) -> str:
    t = now_seoul() + dt.timedelta(seconds=int(sec_from_now))
    hm = f"{t:%H:%M}"
    if not with_day and t.date() == now_seoul().date():
        return f"{hm}쯤"
    return f"{_day_word(t)} {hm}쯤"


def _parse_ts(s: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(s or "", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _registered(job: dict) -> str:
    t = _parse_ts(job.get("created_at"))
    if not t:
        return ""
    day = "오늘" if t.date() == now_seoul().date() else f"{t.month}월 {t.day}일"
    return f"{day} {t:%H:%M} 등록"


def _delete_date(job: dict) -> str:
    t = _parse_ts(job.get("finished_at"))
    if not t:
        return ""
    d = t + dt.timedelta(days=RETENTION_DAYS)
    return f"{d.month}월 {d.day}일"


def _ahead(jobs: list[dict], job: dict) -> list[dict]:
    """이 작업보다 먼저 처리될 작업: 실행 중 + 먼저 등록된 대기 (claim_next 와 같은 순서)."""
    out = [j for j in jobs if j["status"] == J.RUNNING and j["id"] != job["id"]]
    out += [j for j in jobs if j["status"] == J.PENDING and j["id"] < job["id"]]
    return out


def _wait_before(jobs: list[dict], job: dict) -> int:
    return sum(_remaining_sec(j) for j in _ahead(jobs, job))


def _queue_wait(jobs: list[dict]) -> tuple[int, int]:
    """새로 등록하면: (앞에 있는 작업 수, 시작까지 초)."""
    ahead = [j for j in jobs if j["status"] in (J.PENDING, J.RUNNING)]
    return len(ahead), sum(_remaining_sec(j) for j in ahead)


# ──────────────────────────────── 페이지 ────────────────────────────────
def render(cfg: dict) -> None:
    email = current_email()
    if not email:
        st.error("포털을 통해 접속해 주세요. (인증 정보가 없습니다)")
        st.stop()

    store = _store()
    jobs = store.list()

    with st.sidebar:
        st.caption("로그인")
        st.markdown(f"**{email}**")
        b1, b2 = st.columns(2)
        b1.link_button("포털로 이동", PORTAL_URL, use_container_width=True)
        b2.link_button("로그아웃", PORTAL_LOGOUT_URL, use_container_width=True)
        st.divider()
        n_run = sum(1 for j in jobs if j["status"] == J.RUNNING)
        n_wait = sum(1 for j in jobs if j["status"] == J.PENDING)
        st.caption("지금 서버")
        st.markdown(f"**{n_run}** 실행 중 · **{n_wait}** 대기")
        st.caption("NICE 계정은 한 곳에서만 접속되어, 작업을 등록 순서대로 1건씩 처리합니다.")
        st.caption("수집은 서버가 대신 합니다. 탭을 닫아도 작업은 계속됩니다.")

    flash = st.session_state.pop("_flash", None)
    if flash:
        st.toast(flash[0], icon=flash[1])

    st.title("기업정보 조회")
    st.caption("회사 명단 엑셀을 올리면 NICE BizLINE에서 회사 정보를 모아 엑셀로 돌려드립니다.")

    # 탭 전환 요청은 탭 위젯을 만들기 전에만 반영할 수 있다
    goto = st.session_state.pop("_goto_tab", None)
    if goto:
        st.session_state["main_tab"] = goto
    tab_jobs, tab_new = st.tabs([TAB_JOBS, TAB_NEW], key="main_tab", on_change="rerun")
    with tab_jobs:
        render_job_list(store, email)
    with tab_new:
        _render_new_job(store, email)


def _go_to_new_tab() -> None:
    """등록 탭으로 이동. 목록은 fragment 라 앱 전체를 다시 그려야 탭이 바뀐다."""
    st.session_state["_goto_tab"] = TAB_NEW
    st.rerun()


# ──────────────────────────────── 작업 목록 ────────────────────────────────
@st.fragment(run_every="5s")
def render_job_list(store: J.JobStore, email: str) -> None:
    flash = st.session_state.pop("_flash_list", None)
    if flash:
        st.toast(flash[0], icon=flash[1])
    jobs = store.list()
    mine = [j for j in jobs if j.get("owner_email") == email]
    names = {"mine": f"내 작업 {len(mine)}", "all": f"전체 {len(jobs)}"}
    # 선택값은 고정('mine'/'all'), 건수는 표시에만 → 건수가 바뀌어도 선택 유지
    scope_key = st.segmented_control(
        "보기", ["mine", "all"], default="mine", required=True,
        format_func=lambda k: names[k], label_visibility="collapsed", key="scope") or "mine"
    st.caption(f"5초마다 새로고침 · 끝난 작업은 {RETENTION_DAYS}일 뒤 자동 삭제")

    shown = mine if scope_key == "mine" else jobs
    if not shown:
        with st.container(border=True):
            st.markdown(":material/inbox: **아직 등록한 작업이 없습니다.**")
            st.caption('"새 작업 등록"에서 회사 명단 엑셀을 올려 주세요.')
            if st.button("새 작업 등록", type="primary", key="empty_new"):
                _go_to_new_tab()
        return

    for title, statuses in GROUPS:
        group = [j for j in shown if j["status"] in statuses]
        if not group:
            continue
        if title == "끝난 작업":
            group.sort(key=lambda j: j["id"], reverse=True)
        else:
            group.sort(key=lambda j: (statuses.index(j["status"]), j["id"]))
        suffix = f" · {RETENTION_DAYS}일 보관" if title == "끝난 작업" else ""
        st.markdown(f"#### {title} · {len(group)}{suffix}")
        for job in group:
            _job_row(store, jobs, job, email)

    if scope_key == "all" and any(j.get("owner_email") != email for j in shown):
        st.caption("다른 사람의 작업은 순서 확인용입니다. 취소·재개·결과 받기는 올린 사람만 할 수 있습니다.")


def _job_row(store: J.JobStore, jobs: list[dict], job: dict, email: str) -> None:
    s, jid = job["status"], job["id"]
    mine = job.get("owner_email") == email
    meta = STATUS.get(s, STATUS[J.ERROR])
    cancel_req = s == J.RUNNING and store.cancel_requested(jid)
    total, done = _total(job), _done(job)
    who = "나" if mine else _user_id(job.get("owner_email", ""))

    with st.container(border=True):
        c1, c2, c3, c4, c5 = st.columns([1.5, 2.3, 2.4, 1.3, 1.4], vertical_alignment="center")
        label = meta["label"]
        if s == J.PENDING:
            label += f" · 앞에 {len(_ahead(jobs, job))}건"
        with c1:
            st.badge(label, icon=meta["icon"], color=meta["color"])
            if cancel_req:
                st.badge("취소 요청됨", color="gray")
        c2.markdown(f"**{job['name']}**  \n:gray[{who} · {_registered(job)}]")

        with c3:
            if s == J.RUNNING and total:
                now_name = (job.get("progress") or {}).get("name", "")
                st.progress(min(1.0, done / total),
                            text=f"{done:,} / {total:,}" + (f" · 지금 {now_name}" if now_name else ""))
            elif s == J.STOPPED and total:
                st.progress(min(1.0, done / total), text=f"{done:,} / {total:,} · 처리분 저장됨")
            elif s == J.CANCELED:
                st.caption(f"{done:,} / {total:,} · 처리분 저장됨")
            elif s == J.DONE:
                ok = (job.get("counts") or {}).get("success", 0)
                st.markdown(f"**성공 {ok:,}건** :gray[/ 전체 {total:,}]")
            elif s == J.ERROR:
                st.markdown(f":red[{_error_line(job)}]")
            elif done:                               # 이어서 재개로 다시 기다리는 작업
                st.caption(f"{done:,} / {total:,} · 처리분 저장됨")
            else:
                st.caption(f"회사 {total:,}건" if total else "")

        with c4:
            if s == J.RUNNING:
                rem = _remaining_sec(job)
                st.markdown(f"**{_clock(rem, with_day=False)} 완료**  \n:gray[{_human(rem)} 남음]")
            elif s == J.PENDING:
                st.caption(f"{_human(_wait_before(jobs, job))} 뒤 시작")
            elif s == J.STOPPED:
                t = _parse_ts(job.get("finished_at"))
                stop_at = f"{t:%H:%M} 멈춤  \n" if t else ""
                st.caption(f"{stop_at}재개 시 {_human(_remaining_sec(job))}")
            elif s in (J.DONE, J.CANCELED):
                st.caption(f"{_delete_date(job)} 삭제" if _delete_date(job) else "")
            else:
                st.caption("—")

        with c5:
            if not mine:
                st.caption(f"{who} 님의 작업")
            elif cancel_req:
                st.caption("취소 요청됨")
            else:
                _primary_action(store, jobs, job, email, where="row")

        if mine:
            with st.expander("자세히", expanded=s in (J.RUNNING, J.STOPPED)):
                _detail(store, jobs, job, email, cancel_req)


def _error_kind(job: dict) -> str:
    err = job.get("error", "") or ""
    if err.startswith(INPUT_ERRORS):
        return "input"
    if err.startswith(ADMIN_ERRORS):
        return "admin"
    return "runtime"


def _error_line(job: dict) -> str:
    err = (job.get("error", "") or "").strip()
    kind = _error_kind(job)
    if kind == "input":
        if err.startswith("입력이 비어 있음"):
            return "회사 이름이 한 건도 없습니다"
        return "엑셀을 읽지 못했습니다"
    if kind == "admin":
        return "서버 설정 문제로 시작하지 못했습니다"
    return "처리 도중 오류로 멈췄습니다"


def _primary_action(store: J.JobStore, jobs: list[dict], job: dict, email: str, where: str) -> None:
    """상태별 주 버튼 하나. 규칙표의 '주 버튼' 열."""
    s, jid = job["status"], job["id"]
    k = f"{where}_{jid}"
    if s in (J.PENDING, J.RUNNING):
        if st.button("취소", key=f"cancel_{k}"):
            if s == J.PENDING:
                store.request_cancel(jid, email)       # 대기 중 취소는 바로 '취소됨'
                st.rerun(scope="fragment")
            else:
                confirm_cancel(store, job, email)
    elif s == J.STOPPED or (s == J.ERROR and _error_kind(job) == "runtime"):
        _requeue_button(store, jobs, job, email, key=f"rq_{k}", primary=True)
    elif s == J.ERROR and _error_kind(job) == "input":
        if st.button("새로 등록", icon=":material/upload_file:", type="primary", key=f"new_{k}"):
            _go_to_new_tab()
    elif s == J.ERROR:
        st.caption("관리자에게 문의해 주세요")
    elif s in (J.DONE, J.CANCELED):
        _download_button(store, job, email, key=f"dl_{k}", primary=True)


def _requeue_button(store, jobs, job, email, key: str, primary: bool) -> None:
    if st.button("이어서 재개", icon=":material/play_arrow:",
                 type="primary" if primary else "secondary", key=key):
        if store.requeue(job["id"], email):
            start = _clock(_wait_before(store.list(), job), with_day=False)
            st.session_state["_flash_list"] = (f"이어서 하도록 대기열에 넣었습니다. {start} 다시 시작합니다.",
                                               ":material/play_arrow:")
        st.rerun(scope="fragment")


def _download_button(store: J.JobStore, job: dict, email: str, key: str,
                     primary: bool, label: str = "결과 받기") -> None:
    res = store.result_path(job["id"])
    if not os.path.exists(res):
        st.caption("결과 파일 없음")
        return

    def _read() -> bytes:                       # 누를 때만 읽는다 (큰 파일 대비)
        with open(res, "rb") as f:
            return f.read()

    st.download_button(
        label, data=_read, icon=":material/download:",
        type="primary" if primary else "secondary",
        file_name=f"{Path(job['name']).stem}_나이스비즈라인결과.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=key, on_click=lambda jid=job["id"]: store.record_download(jid, email))


def _detail(store: J.JobStore, jobs: list[dict], job: dict, email: str, cancel_req: bool) -> None:
    s, jid, c = job["status"], job["id"], job.get("counts") or {}
    total, done = _total(job), _done(job)

    if s == J.PENDING:
        wait = _wait_before(jobs, job)
        st.info(f"**내 차례를 기다리고 있습니다.**  \n{_human(wait)} 뒤 시작, "
                f"{_clock(wait + _remaining_sec(job))} 완료될 예정입니다. 창을 닫아도 됩니다.",
                icon=":material/schedule:")
    elif s == J.RUNNING:
        now_name = (job.get("progress") or {}).get("name", "")
        body = (f"**서버가 수집하고 있습니다.**  \n{done:,} / {total:,}"
                + (f" · 지금 {now_name}" if now_name else "")
                + f" · {_clock(_remaining_sec(job))} 완료 예정. 창을 닫아도 계속됩니다.")
        if cancel_req:
            body += "  \n취소 요청됨 · 지금 회사까지 처리하고 멈춥니다."
        st.info(body, icon=":material/sync:")
        if not cancel_req:
            st.caption("취소하면 지금 회사까지 처리하고 멈춥니다.")
    elif s == J.DONE:
        st.success(f"**끝났습니다. 쓸 수 있는 결과 {c.get('success', 0):,}건.**  \n"
                   f"결과 파일은 {_delete_date(job)}에 자동 삭제됩니다.",
                   icon=":material/check_circle:")
    elif s == J.STOPPED:
        t = _parse_ts(job.get("finished_at"))
        when = f"{t.month}월 {t.day}일 {t:%H:%M}, " if t else ""
        why = (job.get("stop_reason") or "").strip()
        st.warning(f"**서버가 스스로 멈췄습니다. 취소된 것이 아닙니다.**  \n"
                   f"{when}{why + ' ' if why else ''}안전하게 멈췄습니다. "
                   f"처리한 {done:,}건은 저장되어 있고, 재개하면 {done + 1:,}번째 회사부터 이어서 합니다.",
                   icon=":material/pause_circle:")
        b1, b2, b3 = st.columns([1.1, 1.5, 3], vertical_alignment="center")
        with b1:
            _requeue_button(store, jobs, job, email, key=f"rq_detail_{jid}", primary=True)
        with b2:
            _download_button(store, job, email, key=f"dl_detail_{jid}", primary=False,
                             label="지금까지 결과 받기")
        start = _clock(_wait_before(jobs, job), with_day=False)
        b3.caption(f"남은 {max(0, total - done):,}건 · {_human(_remaining_sec(job))} · "
                   f"재개하면 {start} 시작 (등록 순서 기준)")
    elif s == J.CANCELED:
        st.info(f"**직접 취소한 작업입니다.**  \n처리한 {done:,}건은 저장되어 있습니다. "
                "필요하면 이어서 할 수 있습니다.", icon=":material/cancel:")
        _requeue_button(store, jobs, job, email, key=f"rq_detail_{jid}", primary=False)
    elif s == J.ERROR:
        kind = _error_kind(job)
        err = job.get("error", "") or ""
        if kind == "input" and err.startswith("입력이 비어 있음"):
            msg = "회사 이름이 한 건도 없습니다. 파일을 고쳐 새로 등록해 주세요."
        elif kind == "input":
            msg = ('엑셀을 읽지 못했습니다. 파일이 열리는지, 첫 줄에 "회사명" 열이 있는지 '
                   "확인한 뒤 새로 등록해 주세요.")
        elif kind == "admin":
            msg = "서버 설정 문제로 시작하지 못했습니다. 관리자에게 문의해 주세요."
        else:
            msg = "처리 도중 오류로 멈췄습니다. 처리한 건은 저장되어 있어, 재개하면 이어서 합니다."
        st.error(f"**시작하지 못했습니다:** {msg}" if kind != "runtime" else f"**{msg}**",
                 icon=":material/error:")

    if c:
        _counts(job)

    st.markdown("**진행 기록**")
    st.markdown(_humanize_log(store.tail_log(jid, 400)))
    with st.expander("원문 로그 보기 (최근 40줄)"):
        st.code(store.tail_log(jid, 40) or "로그 없음", language=None)


def _counts(job: dict) -> None:
    c = job.get("counts") or {}
    a, b = st.columns([1, 2.4])
    a.metric("쓸 수 있는 결과", f"{c.get('success', 0):,}건", help='결과 파일 "결과" 시트')
    m = b.columns(4)
    m[0].metric("확인필요", f"{c.get('ambiguous', 0):,}", help='"확인필요" 시트에서 직접 고르기')
    m[1].metric("미발견", f"{c.get('not_found', 0):,}", help='"미발견·오류" 시트')
    m[2].metric("오류", f"{c.get('error', 0):,}", help='"미발견·오류" 시트')
    m[3].metric("필터 제외", f"{c.get('필터제외', 0):,}", help="조건 미달로 결과에서 뺌")
    st.caption(f"처리 {_done(job):,}건 / 입력 {_total(job):,}건")


# ── 로그 → 사람이 읽는 문장 ──
_LOG_RULES = [
    (re.compile(r"작업 시작"), lambda m: "작업 시작"),
    (re.compile(r"자동 재로그인 성공|세션 연장 버튼 없음"), lambda m: "다시 로그인"),
    (re.compile(r"로그인 성공"), lambda m: "NICE BizLINE 로그인"),
    (re.compile(r"세션 연장"), lambda m: "접속 시간 연장"),
    (re.compile(r"재개 시작: (\d+)번째"), lambda m: f"{int(m.group(1)):,}번째 회사부터 이어서 시작"),
    (re.compile(r"체크포인트 저장 \((\d+)건\)"), lambda m: f"{int(m.group(1)):,}건까지 저장"),
    (re.compile(r"연속 (\d+)건 오류"), lambda m: f"연결 오류 {m.group(1)}번 연속 → 안전 정지"),
    (re.compile(r"재로그인이 계속 실패"), lambda m: "다시 로그인 실패 → 안전 정지"),
    (re.compile(r"사용자 중단"), lambda m: "취소 요청으로 멈춤"),
    (re.compile(r"작업 종료 - 상태 done"), lambda m: "작업 완료"),
]
_LOG_LINE = re.compile(r"^(\d{2}:\d{2}):\d{2}\s+\[\w+\]\s+(.*)$")


def _humanize_log(raw: str, limit: int = 6) -> str:
    out: list[str] = []
    for line in raw.splitlines():
        m = _LOG_LINE.match(line.strip())
        if not m:
            continue
        hm, msg = m.groups()
        for rx, fmt in _LOG_RULES:
            mm = rx.search(msg)
            if mm:
                text = fmt(mm)
                # 연속된 같은 문장(예: 접속 시간 연장 반복)은 마지막 것만 남긴다
                if out and out[-1].endswith(f" {text}"):
                    out.pop()
                out.append(f"- `{hm}` {text}")
                break
    return "\n".join(out[-limit:]) or ":gray[아직 기록이 없습니다.]"


@st.dialog("작업을 취소할까요?")
def confirm_cancel(store: J.JobStore, job: dict, email: str) -> None:
    st.write("지금 처리 중인 회사까지 마치고 멈춥니다. 처리한 건은 저장되어 나중에 이어서 할 수 있습니다.")
    st.caption(f"{job['name']} · {_done(job):,} / {_total(job):,} 처리")
    a, b = st.columns(2)
    if a.button("계속 진행", use_container_width=True, key="dlg_keep"):
        st.rerun()
    if b.button("취소하기", type="primary", use_container_width=True, key="dlg_cancel"):
        store.request_cancel(job["id"], email)
        st.rerun()


# ──────────────────────────────── 새 작업 등록 ────────────────────────────────
@st.cache_data
def _template_xlsx() -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "회사목록"
    ws.append(["회사명", "대표자명", "주소", "사업자번호"])
    ws.append(["(주)예시회사", "홍길동", "경기도 평택시 포승읍 ...", "123-45-67890"])
    for col, w in zip("ABCD", (24, 12, 36, 16)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _render_new_job(store: J.JobStore, email: str) -> None:
    jobs = store.list()
    up_key = f"upload_{st.session_state.get('_upload_gen', 0)}"
    left, right = st.columns([1.7, 1], gap="large")

    with left:
        st.markdown("#### 1. 회사 명단 엑셀 올리기")
        uploaded = st.file_uploader(
            "파일 선택 · 또는 여기에 끌어 놓기 · .xlsx 한 개", type=["xlsx"], key=up_key)

    if uploaded is None:
        with left:
            _format_guide()
        with right:
            _time_guide(jobs)
        return

    tmp = Path(tempfile.gettempdir()) / f"preview_{uploaded.file_id}.xlsx"
    tmp.write_bytes(uploaded.getbuffer())
    try:
        companies = read_company_list(str(tmp))
        columns = inspect_columns(str(tmp))
    except Exception:
        with left:
            st.error("엑셀(.xlsx) 파일만 올릴 수 있습니다. 파일이 열리는지 확인해 주세요.")
        return
    if not companies:
        with left:
            st.error("회사 이름이 한 건도 없습니다. 파일 내용을 확인해 주세요.")
        return

    with left:
        st.caption(f"회사 {len(companies):,}건 · {uploaded.size / 1024:,.1f}KB")
        st.markdown("#### 2. 이렇게 읽었습니다")
        used = _column_card(columns, companies)
        st.markdown("#### 3. 옵션")
        narrow = _narrow_options(companies)
        result_filter = _result_filter_options()

    used_cols = [f for f in ("회사명", "대표자명", "주소", "사업자번호") if f in used]
    with right:
        clicked = _register_summary(jobs, len(companies), used_cols, narrow, result_filter)
    if clicked:
        n_ahead, wait = _queue_wait(jobs)
        store.create(
            name=uploaded.name, input_bytes=bytes(uploaded.getbuffer()),
            owner_email=email, input_total=len(companies),
            options={"narrow_fields": narrow or None, "result_filter": result_filter},
        )
        st.session_state["_flash"] = (f"작업을 등록했습니다. {_clock(wait, with_day=False)} 시작합니다.",
                                      ":material/check_circle:")
        st.session_state["_upload_gen"] = st.session_state.get("_upload_gen", 0) + 1
        st.session_state["_goto_tab"] = TAB_JOBS
        st.rerun()


def _format_guide() -> None:
    with st.container(border=True):
        a, b = st.columns([3, 1.3], vertical_alignment="center")
        a.markdown("**이런 엑셀을 올려 주세요**")
        b.download_button("예시 양식 받기", data=_template_xlsx(), icon=":material/download:",
                          file_name="회사목록_예시양식.xlsx", key="tpl_dl",
                          mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        cols = st.columns(4)
        cols[0].markdown("**회사명**  \n:blue[꼭 필요 · 1열]")
        cols[1].markdown("**대표자명**  \n:gray[있으면 더 정확]")
        cols[2].markdown("**주소**  \n:gray[있으면 더 정확]")
        cols[3].markdown("**사업자번호**  \n:gray[있으면 더 정확]")
        st.caption("첫 줄은 열 이름이어야 합니다. 대표자명·주소가 있으면 이름이 같은 회사를 구분할 수 있습니다.")


def _time_guide(jobs: list[dict]) -> None:
    n_ahead, wait = _queue_wait(jobs)
    with st.container(border=True):
        st.markdown("**얼마나 걸리나요?**")
        st.markdown("회사 1건당 20∼30초")
        st.markdown("100건 약 1시간 · 2,000건 약 하루 · 16,000건 4∼5일")
        st.divider()
        if n_ahead:
            st.markdown(f"지금 대기 {n_ahead}건 · 새 작업은 {_human(wait)} 뒤 시작")
        else:
            st.markdown("지금 대기 0건 · 새 작업은 바로 시작")
        st.caption(f"결과 파일은 {RETENTION_DAYS}일 동안 보관됩니다.")


def _column_card(columns: list[dict], companies: list[dict]) -> set[str]:
    """열 인식 결과를 보여 주고, 실제로 쓰는 표준 필드 집합을 돌려준다."""
    used: set[str] = set()
    with st.container(border=True):
        for col in columns:
            field = col["field"]
            orig = col["original"]
            if field in FIELD_USE:
                used.add(field)
                note = FIELD_USE[field]
                if col.get("guessed"):
                    note = "열 이름을 못 찾아 1열을 회사명으로 봤습니다"
                elif col.get("renamed"):
                    note = "이름이 달라 자동으로 맞췄습니다"
                st.markdown(f'엑셀 열 "{orig}" → :material/check: **{field}** :gray[{note}]')
            else:
                st.markdown(f'엑셀 열 "{orig}" → :gray[사용 안 함 · 결과에는 NICE 값이 들어갑니다]')
        missing = [f for f in OPTIONAL_FIELDS if f not in used]
        for f in missing:
            st.caption(f"{f} 열은 없습니다. 있으면 이름이 같은 회사를 더 정확히 구분합니다."
                       if f != "사업자번호" else
                       "사업자번호 열은 없습니다. 있으면 같은 회사인지 더 정확히 확인합니다.")

        n = min(3, len(companies))
        st.markdown(f"**첫 {n}건 미리보기**")
        show = [f for f in ("회사명", "대표자명", "주소", "사업자번호", "전화번호")
                if any(c.get(f) for c in companies[:n])]
        st.dataframe([{k: c.get(k, "") for k in show} for c in companies[:n]],
                     hide_index=True, use_container_width=True)
        st.caption('열을 잘못 읽었다면 열 이름을 "회사명 / 대표자명 / 주소"로 고쳐 다시 올려 주세요.')
    return used


def _narrow_options(companies: list[dict]) -> list[str]:
    narrow: list[str] = []
    with st.container(border=True):
        st.markdown("**이름이 같은 회사 걸러내기**")
        avail = available_filter_fields(companies)
        if not avail:
            st.caption("이 파일에는 대표자명·주소 열이 없어, 이름이 같은 회사는 모두 결과에 담깁니다.")
            return narrow
        labels = {"대표자명": "대표자명이 같은 회사만 남기기", "주소": "주소(지역)가 같은 회사만 남기기"}
        for f in avail:
            if st.checkbox(labels.get(f, f), value=True, key=f"srv_narrow_{f}"):
                narrow.append(f)
        st.caption('예) "(주)동양이엔지"가 사이트에 3곳 있으면, 엑셀 주소와 지역이 같은 곳만 결과에 남깁니다.')
        st.caption("끄면 이름이 같은 회사를 모두 결과에 담습니다.")
    return narrow


def _result_filter_options() -> dict | None:
    with st.container(border=True):
        st.markdown("**결과에 남길 회사 조건 (선택)**")
        c1, c2 = st.columns(2)
        with c1:
            use_emp = st.checkbox("종업원 수 조건", value=False, key="srv_use_emp")
            min_emp = st.number_input("종업원 (명 이상)", min_value=1, value=20,
                                      disabled=not use_emp, key="srv_min_emp")
        with c2:
            use_sales = st.checkbox("매출액 조건", value=False, key="srv_use_sales")
            min_sales = st.number_input("매출액 (억원 이상)", min_value=1, value=10,
                                        disabled=not use_sales, key="srv_min_sales")
        mode = "AND"
        if use_emp and use_sales:
            pick = st.radio("두 조건을", ["모두 충족", "하나만 충족"], horizontal=True, key="srv_mode")
            mode = "OR" if pick == "하나만 충족" else "AND"
        st.caption('조건에 못 미친 회사는 결과 시트에서 빠지고 "필터 제외"로 세어집니다. '
                   '둘 다 켜면 "모두 충족 / 하나만 충족"을 고릅니다.')
    if not (use_emp or use_sales):
        return None
    rf: dict = {"mode": mode}
    if use_emp:
        rf["min_employees"] = int(min_emp)
    if use_sales:
        rf["min_sales"] = int(min_sales) * 100      # 억원 → 백만원
    return rf


def _with_ro(word: str) -> str:
    """조사 '로/으로' 붙이기: 주소로, 대표자명으로 (받침 ㄹ 은 '로')."""
    if not word:
        return word
    ch = ord(word[-1]) - 0xAC00
    if not (0 <= ch < 11172):
        return word + "(으)로"
    jong = ch % 28
    return word + ("으로" if jong not in (0, 8) else "로")


def _register_summary(jobs: list[dict], n: int, used_cols: list[str], narrow: list[str],
                      result_filter: dict | None) -> bool:
    n_ahead, wait = _queue_wait(jobs)
    run = n * SEC_PER_COMPANY
    if result_filter:
        parts = []
        if "min_employees" in result_filter:
            parts.append(f"종업원 {result_filter['min_employees']:,}명 이상")
        if "min_sales" in result_filter:
            parts.append(f"매출액 {result_filter['min_sales'] // 100:,}억원 이상")
        joiner = " 그리고 " if result_filter.get("mode") == "AND" else " 또는 "
        cond = joiner.join(parts)
    else:
        cond = "없음"
    with st.container(border=True):
        st.markdown("**등록 전 확인**")
        st.markdown(
            f"회사 · **{n:,}건**  \n"
            f"사용하는 열 · {', '.join(used_cols)}  \n"
            f"동명 회사 · {_with_ro('·'.join(narrow)) + ' 걸러냄' if narrow else '모두 담음'}  \n"
            f"결과 조건 · {cond}")
        st.divider()
        start = _clock(wait) + (f" · 앞에 {n_ahead}건" if n_ahead else "")
        st.markdown(f"시작 · {start}  \n소요 · {_human(run)}  \n**완료 · {_clock(wait + run)}**")
        clicked = st.button("작업 등록", type="primary", use_container_width=True, key="register")
        st.caption('등록 후 창을 닫아도 됩니다. 진행은 "작업 목록"에서 확인하세요.')
    return clicked
