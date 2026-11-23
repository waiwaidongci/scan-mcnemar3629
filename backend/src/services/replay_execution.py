"""补录回放的后台执行：租约、检查点续跑、双引擎门禁与旧计算拦截。"""

import logging
from datetime import timedelta

from sqlalchemy import and_, or_, select

from src.config.database import Session
from src.constants.enums import Status
from src.domain import replay
from src.models.replay import (
    ReplayRun,
    ReplayShiftResult,
    ReplaySource,
    ReplayTicketResult,
)
from src.services.audit import record
from src.utils.exports import write_replay_report
from src.utils.time import now

log = logging.getLogger(__name__)
LEASE_MINUTES = 3
MAX_ATTEMPTS = 3


def claim_replay():
    stamp = now()
    with Session.begin() as db:
        query = select(ReplayRun).where(
            or_(ReplayRun.status == Status.QUEUED,
                and_(ReplayRun.status == Status.RUNNING, ReplayRun.lease_until < stamp)))
        job = db.scalar(query.order_by(ReplayRun.id).limit(1).with_for_update(skip_locked=True))
        if job is None:
            return None
        if job.attempt >= MAX_ATTEMPTS:
            job.status = Status.FAILED
            job.error = "后台进程多次中断，请重新发起回放"
            job.finished_at = stamp
            record(db, "worker", "replay.failed", job.id, reason=job.error)
            return None
        job.status = Status.RUNNING
        job.attempt += 1
        job.lease_until = stamp + timedelta(minutes=LEASE_MINUTES)
        source = db.get(ReplaySource, job.source_id)
        # 每跑完一个班就把检查点落库；若上次在班次之间中断，这里直接接上待补队列。
        return job.id, job.attempt, source.tickets, source.shifts, job.engine, job.checkpoint


def _run_with_checkpoints(job_id, attempt, tickets, shifts, engine, saved_checkpoint):
    """逐班推进，每个班次结束后提交一次检查点，保证中断可从班次边界续跑。"""
    from src.config.database import Session as SessionFactory

    checkpoint = saved_checkpoint or replay.initial_checkpoint(tickets, shifts, engine)
    total = len(shifts)
    while checkpoint["next_shift"] < total:
        checkpoint = replay.advance(tickets, shifts, engine, checkpoint, shift_budget=1)
        # 独立事务落检查点；即使后续班次崩溃，已完成班次与待补队列也已持久化。
        with SessionFactory.begin() as db:
            job = db.get(ReplayRun, job_id, with_for_update=True)
            if job is None or job.attempt != attempt or job.status != Status.RUNNING:
                return None  # 已被其它执行者接管，当前结果作废。
            job.checkpoint = checkpoint
    return replay.finalize(tickets, shifts, engine, checkpoint)


def _persist_details(db, job, result, boundary):
    # 同一运行可能在中断后重跑，先清理旧明细再写入，保证可重复执行。
    for model in (ReplayTicketResult, ReplayShiftResult):
        old_rows = db.scalars(select(model).where(model.run_id == job.id)).all()
        for old in old_rows:
            db.delete(old)
    db.flush()
    for index, row in enumerate(result["shifts"]):
        db.add(ReplayShiftResult(
            run_id=job.id, shift_index=index, shift_id=row["shift_id"],
            start_at=row["start_at"], end_at=row["end_at"], quota=row["quota"],
            new_arrivals=row["new_arrivals"], processed=row["processed"],
            backlog_end=row["backlog_end"], overdue_open=row["overdue_open"],
            overdue_cumulative=row["overdue_cumulative"], retained=boundary is not None and index < boundary,
        ))
    for row in result["tickets"]:
        db.add(ReplayTicketResult(
            run_id=job.id, ticket_id=row["ticket_id"], arrived_at=row["arrived_at"],
            arrival_shift=row["arrival_shift"], engine=row["engine"], status=row["status"],
            processed_shift=row["processed_shift"],
        ))


def _publish_if_both_ready(db, job):
    sibling = db.scalar(select(ReplayRun).where(ReplayRun.code == job.code, ReplayRun.id != job.id))
    if sibling is None or sibling.status != Status.COMPLETE or not sibling.export_file:
        return False
    if job.stale_blocked or sibling.stale_blocked:
        record(db, "worker", "replay.publish_blocked", job.code, reason="存在晚到的旧计算结果")
        return False
    if not job.published:
        job.published = True
    if not sibling.published:
        sibling.published = True
    record(db, "worker", "replay.published", job.code, engines=sorted([job.engine, sibling.engine]))
    return True


def execute_replay(lease):
    job_id, attempt, stored_tickets, stored_shifts, engine, saved_checkpoint = lease
    failure = export = result = None
    try:
        tickets, shifts = replay.normalize(stored_tickets, stored_shifts)
        result = _run_with_checkpoints(job_id, attempt, tickets, shifts, engine, saved_checkpoint)
        if result is None:
            return  # 执行权已转移，交由新租约处理。
        export = write_replay_report(job_id, attempt, engine, result)
    except ValueError as error:
        failure = str(error)[:500]
    except Exception:
        log.exception("Replay %s failed", job_id)
        failure = "回放计算失败，请检查到票记录与班次名额后重试"

    with Session.begin() as db:
        job = db.get(ReplayRun, job_id, with_for_update=True)
        if job.attempt != attempt or job.status != Status.RUNNING:
            return
        # 旧计算晚到：结果已经发布过，拒绝覆盖，只留痕。
        if job.published:
            job.stale_blocked = True
            record(db, "worker", "replay.stale_blocked", job.id, engine=engine)
            return
        job.lease_until = None
        job.finished_at = now()
        if failure:
            job.status, job.error = Status.FAILED, failure
            record(db, "worker", "replay.failed", job.id, attempt=attempt, reason=failure)
            return
        source = db.get(ReplaySource, job.source_id)
        # 检查点已在逐班循环里落库，这里只写最终结果与明细。
        job.result = result
        job.export_file = export
        job.retained_shift_count = source.boundary if source.boundary is not None else 0
        _persist_details(db, job, result, source.boundary)
        job.status = Status.COMPLETE
        record(db, "worker", "replay.complete", job.id, engine=engine, attempt=attempt)
        _publish_if_both_ready(db, job)
