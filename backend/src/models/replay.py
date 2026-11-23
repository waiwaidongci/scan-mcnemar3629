from datetime import datetime
from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now


class ReplaySource(Base):
    """补录回放数据源的不可变版本。

    每导入一次到票/班次记录就生成一个新版本；``recomputed_from`` 记录它由哪个旧版本
    增量而来，``boundary`` 是最早需要重算的班次下标（等于班次数表示内容无变化）。
    """

    __tablename__ = 'replay_sources'

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    filename: Mapped[str] = mapped_column(String(160))
    checksum: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    version: Mapped[int]
    parent_id: Mapped[int | None] = mapped_column(ForeignKey('replay_sources.id'))
    boundary: Mapped[int | None]
    tickets: Mapped[list] = mapped_column(JSON)
    shifts: Mapped[list] = mapped_column(JSON)
    ticket_count: Mapped[int]
    shift_count: Mapped[int]
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)


class ReplayRun(Base):
    """一次双引擎回放，每个引擎一行，共享同一个 replay_code。"""

    __tablename__ = 'replay_runs'
    __table_args__ = (UniqueConstraint('source_id', 'engine', name='uq_replay_run_source_engine'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey('replay_sources.id'), index=True)
    engine: Mapped[str] = mapped_column(String(2))
    name: Mapped[str] = mapped_column(String(120), default='补录回放')
    status: Mapped[str] = mapped_column(String(20), default='queued', index=True)
    attempt: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None]
    checkpoint: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(500))
    export_file: Mapped[str | None] = mapped_column(String(120))
    # 门禁：两套引擎都跑完且导出后才置 True，控制验收页与导出一次性切换。
    published: Mapped[bool] = mapped_column(default=False, index=True)
    # 旧计算晚到标记：发布后该引擎若再次落盘结果，拒绝发布并留痕。
    stale_blocked: Mapped[bool] = mapped_column(default=False)
    retained_shift_count: Mapped[int] = mapped_column(default=0)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    finished_at: Mapped[datetime | None]


class ReplayShiftResult(Base):
    """各班次回放指标，支持按班次查询积压与超时。"""

    __tablename__ = 'replay_shift_results'
    __table_args__ = (UniqueConstraint('run_id', 'shift_index'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('replay_runs.id'), index=True)
    shift_index: Mapped[int]
    shift_id: Mapped[str] = mapped_column(String(64))
    start_at: Mapped[str] = mapped_column(String(40))
    end_at: Mapped[str] = mapped_column(String(40))
    quota: Mapped[int]
    new_arrivals: Mapped[int]
    processed: Mapped[int]
    backlog_end: Mapped[int]
    overdue_open: Mapped[int]
    overdue_cumulative: Mapped[int]
    retained: Mapped[bool] = mapped_column(default=False)


class ReplayTicketResult(Base):
    """票据级补录轨迹，超时票可按状态检索。"""

    __tablename__ = 'replay_ticket_results'
    __table_args__ = (UniqueConstraint('run_id', 'ticket_id'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('replay_runs.id'), index=True)
    ticket_id: Mapped[str] = mapped_column(String(64), index=True)
    arrived_at: Mapped[str] = mapped_column(String(40))
    arrival_shift: Mapped[str] = mapped_column(String(64))
    engine: Mapped[str] = mapped_column(String(2))
    status: Mapped[str] = mapped_column(String(20), index=True)
    processed_shift: Mapped[str | None] = mapped_column(String(64))
