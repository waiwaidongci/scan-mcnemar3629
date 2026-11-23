"""回放执行编排的离线集成测试。

环境没有第三方依赖，无法直接跑 SQLAlchemy/FastAPI。这里用最小内存桩模拟
ReplayRun / Session / 导出工具，覆盖服务层最容易出错的编排：
- 中断后续跑接续待补队列，结果与一次跑完一致；
- 两套引擎都完成才发布，先完成的处于未发布；
- 发布后晚到的旧计算被拦截且不覆盖结果；
- 重算数据源保留前缀、明细可重复写入。

运行：python3 -I tests/test_replay_orchestration.py
"""

import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 离线环境没有 SQLAlchemy，注入最小桩，仅为加载 src.models.replay 的类定义。
if "sqlalchemy" not in sys.modules:
    sqlalchemy = ModuleType("sqlalchemy")
    sqlalchemy.JSON = "JSON"
    sqlalchemy.ForeignKey = lambda ref: f"FK:{ref}"
    sqlalchemy.String = lambda *a, **k: "String"
    sqlalchemy.Text = "Text"
    sqlalchemy.UniqueConstraint = lambda *a, **k: ("uq", a)

    class _Cond:
        def __init__(self, name):
            self.name = name

        def __eq__(self, other):
            return ("eq", self.name, other)

        def __ne__(self, other):
            return ("ne", self.name, other)

        def __lt__(self, other):
            return ("lt", self.name, other)

        def __gt__(self, other):
            return ("gt", self.name, other)

        def in_(self, values):
            return ("in", self.name, tuple(values))

    def _or(*conds):
        return ("or", None, conds)

    def _and(*conds):
        return ("and", None, conds)

    sqlalchemy.and_ = _and
    sqlalchemy.or_ = _or
    sqlalchemy.create_engine = lambda *a, **k: SimpleNamespace(dispose=lambda: None)

    class _Mapped:
        def __class_getitem__(cls, item):
            return SimpleNamespace()

    def _column(*args, **kwargs):
        return _StubColumn()

    class _StubColumn:
        def __set_name__(self, owner, name):
            owner._stub_columns = getattr(owner, "_stub_columns", ()) + (name,)
            setattr(owner, name, _ColumnCond(name))

    orm = ModuleType("sqlalchemy.orm")

    class _Base:
        _stub_columns = ()

        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    orm.DeclarativeBase = _Base
    orm.Mapped = _Mapped
    orm.mapped_column = _column
    orm.relationship = lambda *a, **k: None

    class _ColumnCond:
        def __init__(self, name):
            self.name = name

        def __eq__(self, other):
            return ("eq", self.name, other)

        def __ne__(self, other):
            return ("ne", self.name, other)

        def __lt__(self, other):
            return ("lt", self.name, other)

        def __gt__(self, other):
            return ("gt", self.name, other)

        def in_(self, values):
            return ("in", self.name, tuple(values))

    class FakeSessionMaker:
        def __call__(self):
            # 真正的共享会话类在文件后部定义，这里延迟引用。
            from tests import test_replay_orchestration as mod
            return mod.FakeSession()

        def begin(self):
            from tests import test_replay_orchestration as mod
            return mod.FakeSession()._ctx()

    orm.sessionmaker = lambda *a, **k: FakeSessionMaker()
    sqlalchemy.orm = orm

    class _Selector:
        def __init__(self, model):
            self.model = model

        def where(self, *conds):
            return FakeQuery(self.model, list(conds))

        def order_by(self, *args):
            return self

        def limit(self, value):
            return FakeQuery(self.model, [], limit=value)

    class FakeQuery:
        def __init__(self, model, conds, limit=None):
            self.model = model
            self.conds = list(conds)
            self._limit = limit

        def where(self, *conds):
            self.conds.extend(conds)
            return self

        def order_by(self, *args):
            return self

        def offset(self, value):
            return self

        def limit(self, value):
            self._limit = value
            return self

        def with_for_update(self, skip_locked=False):
            return self

        @staticmethod
        def _match(row, cond):
            if isinstance(cond, bool):
                return cond  # 桩里无法表达的比较退化为 True，避免误过滤。
            op = cond[0]
            if op == "or":
                return any(FakeQuery._match(row, sub) for sub in cond[2])
            if op == "and":
                return all(FakeQuery._match(row, sub) for sub in cond[2])
            _, name, value = cond
            actual = getattr(row, name, None)
            if op == "eq":
                return actual == value
            if op == "ne":
                return actual != value
            if op == "in":
                return actual in value
            if op == "lt":
                return actual is not None and actual < value
            if op == "gt":
                return actual is not None and actual > value
            return False

        def all(self):
            rows = list(FakeSession.store.get(self.model, []))
            rows = [r for r in rows if all(self._match(r, cond) for cond in self.conds)]
            if self._limit is not None:
                rows = rows[: self._limit]
            return rows

    def select(*models):
        if len(models) == 1 and isinstance(models[0], type):
            return _Selector(models[0])
        return FakeQuery(models[0], [])

    sqlalchemy.select = select
    sys.modules["sqlalchemy"] = sqlalchemy
    sys.modules["sqlalchemy.orm"] = orm

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("JWT_SECRET", "test-secret")

from src.domain import replay as R  # noqa: E402
from src.models.replay import ReplayRun, ReplayShiftResult, ReplaySource, ReplayTicketResult  # noqa: E402
from tests.test_replay_engine import SHIFTS, base_tickets  # noqa: E402


class FakeSession:
    """所有实例共享存储，模拟多个 Session 指向同一数据库。"""

    store = {}
    next_id = 1

    def _ctx(self):
        class Ctx:
            def __enter__(self_inner):
                return self

            def __exit__(self_inner, *exc):
                return False

        return Ctx()

    def begin(self):
        return self._ctx()

    def begin_nested(self):
        return self._ctx()

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = FakeSession.next_id
            FakeSession.next_id += 1
        self.store.setdefault(type(obj), []).append(obj)

    def get(self, model, key, with_for_update=False):
        return next((item for item in self.store.get(model, []) if item.id == key), None)

    def scalar(self, query):
        rows = query.all()
        return rows[0] if rows else None

    def scalars(self, query):
        rows = query.all()

        class _Result(list):
            def all(self_inner):
                return list(self_inner)

        return _Result(rows)

    def delete(self, obj):
        bucket = self.store.get(type(obj), [])
        if obj in bucket:
            bucket.remove(obj)

    def flush(self):
        pass

    def commit(self):
        pass


class FakeSessionFactory:
    """模拟 SQLAlchemy 的 sessionmaker：可调用产出会话，类级 begin 产出上下文。"""

    def __call__(self):
        return FakeSession()

    @staticmethod
    def begin():
        return FakeSession()._ctx()


def make_run(code, source_id, engine, checkpoint=None):
    run = ReplayRun()
    run.id = None
    run.code = code
    run.source_id = source_id
    run.engine = engine
    run.status = "queued"
    run.attempt = 0
    run.lease_until = None
    run.checkpoint = checkpoint
    run.result = None
    run.error = None
    run.export_file = None
    run.published = False
    run.stale_blocked = False
    run.retained_shift_count = 0
    return run


def lease(run, stored_tickets, stored_shifts):
    """模拟 claim_replay：置为 running 并累加 attempt，返回执行器租约元组。"""
    run.status = "running"
    run.attempt += 1
    return (run.id, run.attempt, stored_tickets, stored_shifts, run.engine, run.checkpoint)


def make_source(source_id, stored_tickets, stored_shifts, boundary):
    item = ReplaySource()
    item.id = source_id
    item.tickets = stored_tickets
    item.shifts = stored_shifts
    item.boundary = boundary
    return item


def main():
    tickets_raw, shifts_raw = R.normalize(base_tickets(), SHIFTS)
    stored_tickets, stored_shifts = R.serializable(tickets_raw, shifts_raw)
    source = make_source(1, stored_tickets, stored_shifts, 0)

    with tempfile.TemporaryDirectory() as tmp:
        artifacts = Path(tmp)
        patches = [
            mock.patch("src.services.replay_execution.Session", FakeSessionFactory),
            mock.patch("src.config.database.Session", FakeSessionFactory),
            mock.patch("src.services.replay_execution.write_replay_report",
                       side_effect=lambda run_id, attempt, engine, result: f"replay-{run_id}-{attempt}.csv"),
            mock.patch("src.services.replay_execution.record", lambda *a, **k: None),
            mock.patch("src.config.settings.ARTIFACTS", artifacts),
        ]
        for patch in patches:
            patch.start()

        from src.services import replay_execution as E

        db = FakeSession()
        run_a = make_run("RR-test", 1, "A")
        run_b = make_run("RR-test", 1, "B")
        db.add(run_a)
        db.add(run_b)
        db.add(source)

        # 场景 1：A 先跑完，门禁不应发布。
        E.execute_replay(lease(run_a, stored_tickets, stored_shifts))
        assert run_a.status == "complete", run_a.status
        assert run_a.published is False, "单引擎完成不能发布"
        assert run_a.export_file and run_a.result
        full_checkpoint_a = run_a.checkpoint

        # 场景 2：中断后续跑，检查点须与一次跑完完全一致。
        partial = R.advance(tickets_raw, shifts_raw, "A",
                            R.initial_checkpoint(tickets_raw, shifts_raw, "A"), shift_budget=1)
        run_a2 = make_run("RR-resume", 1, "A", checkpoint=partial)
        db.add(run_a2)
        E.execute_replay(lease(run_a2, stored_tickets, stored_shifts))
        assert run_a2.status == "complete"
        assert run_a2.checkpoint == full_checkpoint_a, "续跑结果必须与一次跑完完全一致"

        # 场景 2b：逐班检查点真实落库——跑到第二班后模拟进程崩溃，续跑接上待补队列。
        run_crash = make_run("RR-crash", 1, "A")
        db.add(run_crash)
        run_crash.status = "running"
        run_crash.attempt = 1
        # 先跑一遍确认逐班检查点循环本身能完整收尾。
        assert E._run_with_checkpoints(
            run_crash.id, 1, tickets_raw, shifts_raw, "A",
            R.initial_checkpoint(tickets_raw, shifts_raw, "A")) is not None
        # 人为回滚到「只完成两班」的检查点并把状态改回排队，模拟进程在班次之间崩溃。
        two_shift_cp = R.advance(tickets_raw, shifts_raw, "A",
                                 R.initial_checkpoint(tickets_raw, shifts_raw, "A"), shift_budget=2)
        run_crash.checkpoint = two_shift_cp
        run_crash.status = "queued"
        run_crash.result = None
        # 模拟 worker 重新领取（attempt 变成 2），续跑必须从中断点接着算。
        E.execute_replay(lease(run_crash, stored_tickets, stored_shifts))
        assert run_crash.status == "complete"
        assert run_crash.attempt == 2
        assert run_crash.checkpoint == full_checkpoint_a, "崩溃续跑的最终检查点须与完整运行一致"
        assert run_crash.result["summary"] == run_a.result["summary"]

        # 场景 3：B 完成，双引擎门禁一次性发布。
        E.execute_replay(lease(run_b, stored_tickets, stored_shifts))
        assert run_b.status == "complete"
        assert run_a.published and run_b.published, "两套都完成后应同时发布"

        # 场景 4：发布后晚到的旧计算必须被拦截，结果不被覆盖。
        original_result = run_b.result
        original_export = run_b.export_file
        run_b.status = "running"  # 模拟一个迟到的重试租约
        run_b.attempt += 1
        E.execute_replay((run_b.id, run_b.attempt, stored_tickets, stored_shifts, "B", None))
        assert run_b.stale_blocked is True, "晚到旧计算应被标记拦截"
        assert run_b.result is original_result, "旧计算不得覆盖已发布结果"
        assert run_b.export_file is original_export

        # 场景 5：坏数据走失败路径，不发布。
        run_bad = make_run("RR-bad", 1, "A")
        db.add(run_bad)
        E.execute_replay(lease(run_bad, [], stored_shifts))
        assert run_bad.status == "failed" and run_bad.error and run_bad.published is False

        # 场景 6：增量重算保留前缀，明细幂等。
        grown = base_tickets() + [
            {"ticket_id": "T06", "arrived_at": "2026-11-21 11:00:00", "a_pass": False, "b_pass": False},
        ]
        old_t, old_s = R.normalize(base_tickets(), SHIFTS)
        new_t, new_s = R.normalize(grown, SHIFTS)
        boundary = R.recompute_boundary(old_t, old_s, new_t, new_s)
        assert boundary == 1
        new_stored_t, new_stored_s = R.serializable(new_t, new_s)
        source2 = make_source(2, new_stored_t, new_stored_s, boundary)
        run_inc = make_run("RR-inc", 2, "A")
        db.add(run_inc)
        db.add(source2)
        E.execute_replay(lease(run_inc, new_stored_t, new_stored_s))
        assert run_inc.retained_shift_count == 1

        from src.models.replay import ReplayShiftResult, ReplayTicketResult
        shift_rows = [item for item in FakeSession.store.get(ReplayShiftResult, []) if item.run_id == run_inc.id]
        ticket_rows = [item for item in FakeSession.store.get(ReplayTicketResult, []) if item.run_id == run_inc.id]
        assert len(shift_rows) == len(SHIFTS)
        assert len(ticket_rows) == sum(1 for t in grown if not t["a_pass"])
        assert shift_rows[0].retained is True
        assert all(not row.retained for row in shift_rows[1:])

        # 重复执行先清旧明细再写，不产生重复行。
        E._persist_details(db, run_inc, run_inc.result, boundary)
        shift_rows = [item for item in FakeSession.store.get(ReplayShiftResult, []) if item.run_id == run_inc.id]
        assert len(shift_rows) == len(SHIFTS), "明细必须可重复写入而不重复"

        for patch in patches:
            patch.stop()

    print("orchestration OK: 续跑一致、双引擎门禁、旧计算拦截、增量保留、明细幂等 全部通过")


if __name__ == "__main__":
    main()
