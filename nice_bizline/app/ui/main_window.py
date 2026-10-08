"""나이스비즈라인 자동화 메인 윈도우 (Tkinter).

UI 스레드와 수집 워커 스레드를 분리하여 UI 멈춤을 방지합니다.
워커는 콜백으로 진행률/로그를 보내고, UI는 after()로 안전하게 위젯을 갱신합니다.
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
from datetime import datetime
from nice_bizline.app.core.timeutil import now_seoul
from tkinter import filedialog, messagebox, ttk

from ..core import checkpoint, credentials, file_logger
from ..core.collector import MockCollector, NiceBizlineCollector
from ..core.worker import RunOptions, Worker, WorkerCallbacks
from ..excelio.reader import read_company_list
from ..excelio.writer import write_results


class MainWindow(tk.Tk):
    def __init__(self, config: dict, mock: bool = False):
        super().__init__()
        self._cfg = config
        self._mock = mock
        self._worker: Worker | None = None
        self._companies: list[dict] = []
        self._input_path: str = ""
        self._output_path: str = ""
        self._log_path: str = ""
        self._run_logger = None

        self.title("나이스비즈라인 기업정보 조회 자동화" + ("  [모의모드]" if mock else ""))
        self.minsize(720, 640)
        self._build_ui()

    # ── UI 구성 ──
    def _build_ui(self) -> None:
        PADX, PADY = 14, 8
        BG = "#F5F7FA"
        HEADER_BG = "#1F4E79"

        self.configure(bg=BG)

        header = tk.Frame(self, bg=HEADER_BG)
        header.pack(fill="x")
        tk.Label(
            header, text="  나이스비즈라인 기업정보 조회 자동화",
            bg=HEADER_BG, fg="white",
            font=("맑은 고딕", 13, "bold"), pady=12,
        ).pack(side="left")

        body = tk.Frame(self, bg=BG, padx=PADX, pady=PADY)
        body.pack(fill="both", expand=True)

        # ① 입력 파일
        grp1 = ttk.LabelFrame(body, text=" ① 입력 파일 ", padding=8)
        grp1.pack(fill="x", pady=(6, 4))
        self._input_var = tk.StringVar()
        ttk.Entry(grp1, textvariable=self._input_var, state="readonly", width=58).grid(
            row=0, column=0, padx=(0, 6), sticky="ew")
        ttk.Button(grp1, text="파일 선택", width=12, command=self._browse).grid(row=0, column=1)
        self._input_info = tk.StringVar(value="")
        tk.Label(grp1, textvariable=self._input_info, bg=BG,
                 font=("맑은 고딕", 9), fg="#555").grid(row=1, column=0, columnspan=2,
                                                       sticky="w", pady=(4, 0))
        grp1.columnconfigure(0, weight=1)

        # ② 계정
        grp2 = ttk.LabelFrame(body, text=" ② 계정 ", padding=8)
        grp2.pack(fill="x", pady=4)
        tk.Label(grp2, text="아이디", bg=BG).grid(row=0, column=0, sticky="w")
        self._id_var = tk.StringVar()
        ttk.Entry(grp2, textvariable=self._id_var, width=20).grid(row=0, column=1, padx=(4, 16))
        tk.Label(grp2, text="비밀번호", bg=BG).grid(row=0, column=2, sticky="w")
        self._pw_var = tk.StringVar()
        ttk.Entry(grp2, textvariable=self._pw_var, width=20, show="*").grid(row=0, column=3, padx=4)
        self._save_creds_var = tk.BooleanVar(value=False)
        self._save_chk = ttk.Checkbutton(
            grp2, text="저장 (OS 자격증명)",
            variable=self._save_creds_var,
        )
        self._save_chk.grid(row=0, column=4, padx=(12, 0))
        self._creds_info = tk.StringVar(value="")
        tk.Label(grp2, textvariable=self._creds_info, bg=BG,
                 font=("맑은 고딕", 9), fg="#555").grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(4, 0))

        self._load_saved_credentials()

        # ③ 옵션
        grp3 = ttk.LabelFrame(body, text=" ③ 옵션 ", padding=8)
        grp3.pack(fill="x", pady=4)
        # 실사이트는 최신 결산 1개년만 제공 → 1개년 고정
        tk.Label(grp3, text="재무 범위:", bg=BG).grid(row=0, column=0, sticky="w")
        self._years_var = tk.IntVar(value=1)
        tk.Label(grp3, text="최신 결산 1개년 (사이트 제공 기준)", bg=BG).grid(
            row=0, column=1, padx=8, sticky="w")

        # 실행 컨트롤
        ctrl = tk.Frame(body, bg=BG)
        ctrl.pack(fill="x", pady=10)
        self._run_btn = tk.Button(
            ctrl, text="▶  조회 시작", command=self._on_start,
            bg="#1F4E79", fg="white", activebackground="#163A5F",
            font=("맑은 고딕", 11, "bold"),
            relief="flat", padx=20, pady=8, cursor="hand2",
        )
        self._run_btn.pack(side="left", padx=(0, 6))
        self._stop_btn = tk.Button(
            ctrl, text="■  중단", command=self._on_stop, state="disabled",
            bg="#888", fg="white", font=("맑은 고딕", 11),
            relief="flat", padx=16, pady=8, cursor="hand2",
        )
        self._stop_btn.pack(side="left")

        # ④ 진행 상황
        grp4 = ttk.LabelFrame(body, text=" ④ 진행 상황 ", padding=8)
        grp4.pack(fill="x", pady=4)
        self._progress = ttk.Progressbar(grp4, mode="determinate", length=560)
        self._progress.pack(fill="x", pady=(2, 4))
        self._status_var = tk.StringVar(value="대기 중")
        tk.Label(grp4, textvariable=self._status_var, bg=BG,
                 font=("맑은 고딕", 9), fg="#333", anchor="w").pack(fill="x")

        # ⑤ 로그
        grp5 = ttk.LabelFrame(body, text=" ⑤ 로그 ", padding=6)
        grp5.pack(fill="both", expand=True, pady=(4, 6))
        self._log = tk.Text(
            grp5, height=14, state="disabled",
            bg="#1E1E1E", fg="#D4D4D4", font=("Consolas", 9),
            relief="flat", insertbackground="white",
        )
        scroll = ttk.Scrollbar(grp5, command=self._log.yview)
        self._log.configure(yscrollcommand=scroll.set)
        self._log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        # 로그 색상 태그
        self._log.tag_configure("info", foreground="#D4D4D4")
        self._log.tag_configure("warn", foreground="#FFB454")
        self._log.tag_configure("error", foreground="#F48771")

        # ⑥ 완료 후 액션
        done = tk.Frame(body, bg=BG)
        done.pack(fill="x", pady=(0, 4))
        self._open_file_btn = ttk.Button(done, text="결과 파일 열기",
                                         command=self._open_result_file, state="disabled")
        self._open_file_btn.pack(side="left", padx=(0, 6))
        self._open_folder_btn = ttk.Button(done, text="폴더 열기",
                                           command=self._open_folder, state="disabled")
        self._open_folder_btn.pack(side="left", padx=(0, 6))
        self._open_log_btn = ttk.Button(done, text="로그 파일 열기",
                                        command=self._open_log_file, state="disabled")
        self._open_log_btn.pack(side="left")

    # ── 파일 선택 ──
    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            title="회사 목록 엑셀 선택",
            filetypes=[("Excel 파일", "*.xlsx"), ("모든 파일", "*.*")],
            initialdir=os.getcwd(),
        )
        if not path:
            return
        try:
            self._companies = read_company_list(path)
        except Exception as e:
            messagebox.showerror("입력 파일 오류", f"엑셀을 읽지 못했습니다:\n{e}")
            return
        self._input_path = path
        self._input_var.set(path)
        self._input_info.set(f"  → 불러온 회사 수: {len(self._companies)}건")

    # ── 자격증명 ──
    def _load_saved_credentials(self) -> None:
        if not credentials.is_available():
            self._creds_info.set("  → 저장소(keyring) 없음 - 매 실행 시 입력 필요")
            self._save_chk.state(["disabled"])
            return
        uid, pw = credentials.load()
        if uid:
            self._id_var.set(uid)
            self._pw_var.set(pw)
            self._save_creds_var.set(True)
            self._creds_info.set("  → 저장된 계정 불러옴 (체크 해제 후 시작하면 삭제됩니다)")
        else:
            self._creds_info.set("")

    def _persist_credentials(self) -> None:
        if not credentials.is_available():
            return
        if self._save_creds_var.get():
            ok = credentials.save(self._id_var.get(), self._pw_var.get())
            if ok:
                self._append_log("info", "계정을 OS 자격증명 저장소에 저장했습니다.")
        else:
            credentials.clear()

    # ── 시작/중단 ──
    def _on_start(self) -> None:
        if not self._input_path or not self._companies:
            messagebox.showwarning("입력 필요", "입력 엑셀 파일을 먼저 선택하세요.")
            return
        if not self._mock and (not self._id_var.get() or not self._pw_var.get()):
            messagebox.showwarning("계정 필요", "아이디/비밀번호를 입력하세요.")
            return

        resume = self._ask_resume_if_checkpoint()
        if resume is None:
            return  # 사용자 취소

        self._set_running(True)
        self._log_clear()
        self._progress.configure(maximum=len(self._companies), value=0)

        # 파일 로거 초기화 (실패해도 앱 계속 동작)
        try:
            self._run_logger, self._log_path = file_logger.setup_run_logger(self._input_path)
            self._append_log("info", f"로그 파일: {self._log_path}")
        except Exception as e:
            self._run_logger, self._log_path = None, ""
            self._append_log("warn", f"파일 로깅 비활성화: {e}")

        self._append_log("info", f"수집 시작 - {len(self._companies)}건"
                         + ("  (이전 진행에서 재개)" if resume else ""))
        self._persist_credentials()

        collector = MockCollector(self._cfg) if self._mock else NiceBizlineCollector(self._cfg)

        cb = WorkerCallbacks(
            on_log=self._safe_log,
            on_progress=self._safe_progress,
            on_done=self._safe_done,
        )
        opts = RunOptions(
            user_id=self._id_var.get(),
            password=self._pw_var.get(),
            companies=self._companies,
            finance_years=self._years_var.get(),
            input_path=self._input_path,
            resume=resume,
        )
        self._worker = Worker(collector, self._cfg, opts, cb)
        self._worker.start()

    def _ask_resume_if_checkpoint(self) -> bool | None:
        """체크포인트가 있으면 사용자에게 선택을 받음.

        반환: True(재개) / False(처음부터) / None(취소)
        """
        if not checkpoint.exists(self._input_path):
            return False
        state = checkpoint.load(self._input_path) or {}
        summary = checkpoint.summarize(state)
        dlg = messagebox.askyesnocancel(
            "이전 진행 발견",
            f"이 입력 파일의 이전 진행 상태가 있습니다.\n\n  {summary}\n\n"
            "[예] 이어서 진행 (이미 처리한 회사 스킵)\n"
            "[아니오] 처음부터 새로 시작 (체크포인트 삭제)\n"
            "[취소] 시작하지 않음",
        )
        if dlg is None:
            return None
        if dlg is False:
            checkpoint.clear(self._input_path)
        return bool(dlg)

    def _on_stop(self) -> None:
        if self._worker and self._worker.is_alive():
            self._append_log("warn", "중단 요청 - 현재 회사 처리 후 정지합니다.")
            self._worker.stop()
            self._stop_btn.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        if running:
            self._run_btn.configure(text="조회 중...", state="disabled", bg="#555")
            self._stop_btn.configure(state="normal", bg="#B33A3A")
            self._open_file_btn.configure(state="disabled")
            self._open_folder_btn.configure(state="disabled")
            self._open_log_btn.configure(state="disabled")
        else:
            self._run_btn.configure(text="▶  조회 시작", state="normal", bg="#1F4E79")
            self._stop_btn.configure(state="disabled", bg="#888")

    # ── 스레드 → UI (안전 갱신) ──
    def _safe_log(self, level: str, msg: str) -> None:
        self.after(0, self._append_log, level, msg)

    def _safe_progress(self, current: int, total: int, name: str) -> None:
        self.after(0, self._update_progress, current, total, name)

    def _safe_done(self, summary: dict) -> None:
        self.after(0, self._on_worker_done, summary)

    def _append_log(self, level: str, msg: str) -> None:
        ts = now_seoul().strftime("%H:%M:%S")
        self._log.configure(state="normal")
        self._log.insert("end", f"{ts}  {msg}\n", level)
        self._log.see("end")
        self._log.configure(state="disabled")
        if self._run_logger is not None:
            try:
                file_logger.write(self._run_logger, level, msg)
            except Exception:
                pass  # 로깅 실패가 앱을 멈추지 않게

    def _log_clear(self) -> None:
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    def _update_progress(self, current: int, total: int, name: str) -> None:
        self._progress.configure(value=current)
        pct = int(current / total * 100) if total else 0
        self._status_var.set(f"{current} / {total}  ({pct}%)   현재: {name}")

    def _on_worker_done(self, summary: dict) -> None:
        self._set_running(False)
        if not self._worker:
            return

        # 결과 저장
        base, _ = os.path.splitext(self._input_path)
        out = f"{base}_나이스비즈라인결과_{now_seoul().strftime('%Y%m%d_%H%M')}.xlsx"
        try:
            write_results(
                out,
                records=self._worker.state.records,
                unfound=self._worker.state.unfound,
                ambiguous=self._worker.state.ambiguous,
                summary=summary,
                finance_years=self._years_var.get(),
            )
            self._output_path = out
            self._open_file_btn.configure(state="normal")
            self._open_folder_btn.configure(state="normal")
            self._append_log("info", f"결과 저장 완료: {out}")
            # 중단된 경우에는 체크포인트 유지(다음에 재개 가능), 정상 완료 시 정리
            if not summary.get("stopped"):
                checkpoint.clear(self._input_path)
                self._append_log("info", "체크포인트 정리 완료")
        except Exception as e:
            self._append_log("error", f"결과 저장 실패: {e}")

        # 파일 로거 닫기
        if self._run_logger is not None:
            file_logger.close(self._run_logger)
            self._run_logger = None
            if self._log_path and os.path.exists(self._log_path):
                self._open_log_btn.configure(state="normal")

        msg = (
            f"성공 {summary.get('success', 0)} / "
            f"미발견 {summary.get('not_found', 0)} / "
            f"확인필요 {summary.get('ambiguous', 0)} / "
            f"오류 {summary.get('error', 0)}"
        )
        self._status_var.set(f"완료 - {msg}")
        messagebox.showinfo("완료", msg)

    def _open_result_file(self) -> None:
        if not self._output_path or not os.path.exists(self._output_path):
            return
        _open_path(self._output_path)

    def _open_folder(self) -> None:
        if not self._output_path:
            return
        _open_path(os.path.dirname(os.path.abspath(self._output_path)))

    def _open_log_file(self) -> None:
        if not self._log_path or not os.path.exists(self._log_path):
            return
        _open_path(self._log_path)


def _open_path(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        os.system(f'open "{path}"')
    else:
        os.system(f'xdg-open "{path}"')
