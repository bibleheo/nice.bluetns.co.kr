"""백그라운드 수집 워커 - pipeline generator를 스레드에서 소비, 콜백으로 UI에 전달."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from .pipeline import PipelineOptions, PipelineState, run_pipeline


@dataclass
class WorkerCallbacks:
    on_log: Callable[[str, str], None]              # (level, message)  level: info/warn/error
    on_progress: Callable[[int, int, str], None]    # (current, total, company_name)
    on_done: Callable[[dict], None]                 # summary dict


@dataclass
class RunOptions:
    user_id: str
    password: str
    companies: list[dict]
    finance_years: int = 1
    input_path: str = ""
    resume: bool = False
    checkpoint_every: int = 10
    narrow_fields: list | None = None
    result_filter: dict | None = None


@dataclass
class RunState:
    records: list[dict] = field(default_factory=list)
    unfound: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    processed_keys: set = field(default_factory=set)


class Worker(threading.Thread):
    """수집 파이프라인을 스레드에서 실행. stop()으로 안전 정지."""

    def __init__(self, collector, config: dict, options: RunOptions, callbacks: WorkerCallbacks):
        super().__init__(daemon=True)
        self._collector = collector
        self._cfg = config
        self._opts = options
        self._cb = callbacks
        self._stop_event = threading.Event()
        self.state = RunState()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        opts = PipelineOptions(
            user_id=self._opts.user_id,
            password=self._opts.password,
            companies=self._opts.companies,
            finance_years=self._opts.finance_years,
            input_path=self._opts.input_path,
            resume=self._opts.resume,
            checkpoint_every=self._opts.checkpoint_every,
            narrow_fields=self._opts.narrow_fields,
            result_filter=self._opts.result_filter,
        )
        pstate = PipelineState()

        for event in run_pipeline(self._collector, self._cfg, opts, pstate,
                                  stop_check=self._stop_event.is_set):
            t = event["type"]
            if t == "log":
                self._cb.on_log(event["level"], event["message"])
            elif t == "progress":
                self._cb.on_progress(event["current"], event["total"], event["name"])
            elif t == "done":
                self.state.records = pstate.records
                self.state.unfound = pstate.unfound
                self.state.ambiguous = pstate.ambiguous
                self.state.processed_keys = pstate.processed_keys
                self.state.summary = pstate.summary
                self._cb.on_done(pstate.summary)
