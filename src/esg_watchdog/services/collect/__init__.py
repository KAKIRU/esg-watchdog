"""F-01 수집 서비스 묶음 — 뉴스(news) · DART 후속공시(filings) · 보고서 PDF(reports) · KRX(옵션 경로).

여기에는 세 서비스가 같이 쓰는 pipeline_runs 기록 헬퍼만 둔다.
- trigger 는 전부 'manual' (cron 없음, D-28). status 는 success / partial / failed.
- DB·설정 import 는 함수 안에서만 한다 — .env 없이도 서비스 모듈을 import 할 수 있어야 한다(테스트는 가짜 client 주입).
"""

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
TRIGGER = "manual"


def now_kst() -> datetime:
    return datetime.now(KST)


def decide_status(succeeded: int, failed: int) -> str:
    """회사 단위 성공/실패 수로 실행 상태를 정한다. 실패 0 → success, 성공 0 → failed, 섞이면 partial."""
    if failed == 0:
        return "success"
    if succeeded == 0:
        return "failed"
    return "partial"


def failure_note(failed_labels: Sequence[str]) -> str | None:
    """pipeline_runs.note — 실패한 회사 목록. 없으면 None."""
    return f"실패: {', '.join(failed_labels)}" if failed_labels else None


def start_run(stage: str) -> int:
    """pipeline_runs 에 running 행을 만들고 id 를 돌려준다."""
    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import PipelineRun

    with SessionLocal() as session:
        run = PipelineRun(trigger=TRIGGER, stage=stage, status="running", stage_stats={})
        session.add(run)
        session.commit()
        return run.id


def finish_run(run_id: int, status: str, stage_stats: dict, note: str | None = None) -> None:
    """실행 결과를 기록한다. stage_stats 는 JSONB 로 그대로 들어간다(값은 JSON 직렬화 가능해야 한다)."""
    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import PipelineRun

    with SessionLocal() as session:
        run = session.get(PipelineRun, run_id)
        if run is None:
            return
        run.status = status
        run.stage_stats = stage_stats
        run.note = note
        run.finished_at = now_kst()
        session.commit()
