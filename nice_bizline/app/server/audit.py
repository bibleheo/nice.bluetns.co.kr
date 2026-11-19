"""보안 점검용 이용 기록 요약 (계정 탈취 흔적 찾기).

  python -m nice_bizline.app.server.audit [--domain bluetns.co.kr]

작업 큐(DATA_DIR/jobs/*/job.json)에 남은 기록만 읽는다. 바꾸는 것은 없다.
- 이메일별: 등록 작업 수, 입력 회사 수, 결과 다운로드 수, 처음·마지막 활동 시각
- 경고: 회사 도메인이 아닌 이메일, 올린 사람이 아닌 사람의 다운로드,
        짧은 시간에 몰린 다운로드(대량 반출 의심)
작업 내용(파일명·회사명)은 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from collections import defaultdict

from . import jobs as J

BURST_WINDOW_MIN = 60       # 이 시간 안에
BURST_COUNT = 10            # 이만큼 넘게 내려받으면 경고


def _ts(s: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(s or "", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def summarize(store: J.JobStore, domain: str) -> tuple[list[dict], list[str]]:
    users: dict[str, dict] = defaultdict(lambda: {"jobs": 0, "companies": 0, "downloads": 0,
                                                  "first": None, "last": None})
    warnings: list[str] = []
    downloads_by_email: dict[str, list[dt.datetime]] = defaultdict(list)

    def touch(email: str, when: dt.datetime | None) -> None:
        u = users[email]
        if when:
            u["first"] = min(filter(None, [u["first"], when]))
            u["last"] = max(filter(None, [u["last"], when]))

    for job in store.list():
        owner = (job.get("owner_email") or "").strip().lower()
        u = users[owner]
        u["jobs"] += 1
        u["companies"] += int(job.get("input_total") or (job.get("counts") or {}).get("total") or 0)
        touch(owner, _ts(job.get("created_at")))
        for d in job.get("downloads") or []:
            who = (d.get("email") or "").strip().lower()
            when = _ts(d.get("at"))
            users[who]["downloads"] += 1
            touch(who, when)
            if when:
                downloads_by_email[who].append(when)
            if who != owner:
                warnings.append(f"올린 사람이 아닌 다운로드: 작업 {job['id']} 올린 사람 {owner}, "
                                f"내려받은 사람 {who} ({d.get('at')})")

    for email in users:
        if not email.endswith("@" + domain):
            warnings.append(f"회사 도메인이 아닌 이메일: {email or '(비어 있음)'}")
    for email, times in downloads_by_email.items():
        times.sort()
        win = dt.timedelta(minutes=BURST_WINDOW_MIN)
        for i, t in enumerate(times):
            n = sum(1 for x in times[i:] if x - t <= win)
            if n > BURST_COUNT:
                warnings.append(f"짧은 시간 대량 다운로드: {email} {t:%Y-%m-%d %H:%M}부터 "
                                f"{BURST_WINDOW_MIN}분 안에 {n}회")
                break

    rows = [{"email": e or "(비어 있음)", **v} for e, v in users.items()]
    rows.sort(key=lambda r: r["last"] or dt.datetime.min, reverse=True)
    return rows, warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--domain", default="bluetns.co.kr", help="회사 이메일 도메인")
    args = ap.parse_args(argv)
    store = J.JobStore(os.environ.get("DATA_DIR", "/data"))
    rows, warnings = summarize(store, args.domain.lower())

    print(f"[이용 기록] 이메일 {len(rows)}개 (작업 큐 기준, 보관 기간 지난 작업은 이미 삭제됨)")
    print(f"{'이메일':<36} {'작업':>4} {'회사 수':>8} {'다운로드':>6}  처음 ~ 마지막")
    for r in rows:
        first = f"{r['first']:%m-%d %H:%M}" if r["first"] else "-"
        last = f"{r['last']:%m-%d %H:%M}" if r["last"] else "-"
        print(f"{r['email']:<36} {r['jobs']:>4} {r['companies']:>8,} {r['downloads']:>6}  {first} ~ {last}")
    print()
    if warnings:
        print(f"[경고] {len(warnings)}건")
        for w in warnings:
            print(" - " + w)
    else:
        print("[경고] 없음")
    return 1 if warnings else 0


if __name__ == "__main__":
    sys.exit(main())
