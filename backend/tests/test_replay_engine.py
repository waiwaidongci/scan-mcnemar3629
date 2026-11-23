"""补录回放引擎的离线单元测试，只用标准库：python3 -I tests/test_replay_engine.py -v。"""

import sys
import unittest
from datetime import datetime
from pathlib import Path

# 隔离模式（-I）不自动加入当前目录，这里程序化补上 backend 根目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.domain import replay as R


def dt(text):
    return datetime.fromisoformat(text.replace(" ", "T"))


def ticket(ticket_id, arrived, a_pass, b_pass):
    return {"ticket_id": ticket_id, "arrived_at": arrived, "a_pass": a_pass, "b_pass": b_pass}


def shift(shift_id, start, end, quota_a, quota_b):
    return {"shift_id": shift_id, "start_at": start, "end_at": end,
            "quota_a": quota_a, "quota_b": quota_b}


# 三班，每班每引擎 2 个名额。
SHIFTS = [
    shift("S1", "2026-11-21 08:00:00", "2026-11-21 10:00:00", 2, 2),
    shift("S2", "2026-11-21 10:00:00", "2026-11-21 12:00:00", 2, 2),
    shift("S3", "2026-11-21 13:00:00", "2026-11-21 15:00:00", 2, 2),
]


def base_tickets():
    # 五张票同刻到达 S1，票号 T01..T05；A 全失败，B 通过前两张（T01、T02）。
    return [
        ticket(f"T0{i}", "2026-11-21 09:00:00", False, i <= 2) for i in range(1, 6)
    ]


def checkpoint_of(raw_tickets, raw_shifts, engine):
    tickets, shifts = R.normalize(raw_tickets, raw_shifts)
    return R.advance(tickets, shifts, engine, R.initial_checkpoint(tickets, shifts, engine))


_, SHIFTS_N = R.normalize(base_tickets(), SHIFTS)


class EngineTests(unittest.TestCase):
    def test_carryover_and_ordering(self):
        result = R.simulate(base_tickets(), SHIFTS, "A")
        cp = checkpoint_of(base_tickets(), SHIFTS, "A")
        s1, s2, s3 = result["shifts"]
        # S1 处理 2 张，结转 3 张；按票号顺序先 T01、T02。
        self.assertEqual(s1["new_arrivals"], 5)
        self.assertEqual(s1["processed"], 2)
        self.assertEqual(s1["backlog_end"], 3)
        # 对外结果不含明细票号，处理顺序保存在检查点里（续跑依据）。
        self.assertEqual(cp["results"][0]["processed_ids"], ["T01", "T02"])
        self.assertEqual(cp["results"][1]["processed_ids"], ["T03", "T04"])
        self.assertEqual(cp["results"][2]["processed_ids"], ["T05"])
        self.assertEqual(s3["backlog_end"], 0)

    def test_overdue_marked_after_next_shift(self):
        result = R.simulate(base_tickets(), SHIFTS, "A")
        s1, s2, s3 = result["shifts"]
        # S1 到票，S2 结束仍积压 1 张（T05）→ S2 起记超时。
        self.assertEqual(s1["overdue_cumulative"], 0)
        self.assertEqual(s2["overdue_cumulative"], 1)
        self.assertEqual(s2["overdue_open"], 1)
        # S3 补录后仍保留超时标记。
        self.assertEqual(s3["overdue_cumulative"], 1)
        status = {row["ticket_id"]: row["status"] for row in result["tickets"]}
        self.assertEqual(status["T05"], R.DONE_OVERDUE)
        self.assertEqual(status["T01"], R.DONE)
        self.assertEqual(result["summary"]["overdue_total"], 1)

    def test_engine_b_smaller_failed_pool(self):
        result_b = R.simulate(base_tickets(), SHIFTS, "B")
        result_a = R.simulate(base_tickets(), SHIFTS, "A")
        # B 通过 T01、T02，失败池只有 T03..T05 三张。
        self.assertEqual(result_b["summary"]["failed_total"], 3)
        self.assertEqual(result_b["summary"]["overdue_total"], 0)
        self.assertEqual(result_a["summary"]["overdue_total"], 1)
        merged = R.compare(result_a, result_b)
        s2 = merged[1]
        self.assertEqual(s2["backlog_a"], 1)
        self.assertEqual(s2["backlog_b"], 0)
        self.assertEqual(s2["backlog_delta"], -1)

    def test_same_timestamp_tie_breaks_by_ticket_id(self):
        tickets = [
            ticket("T99", "2026-11-21 09:00:00", False, False),
            ticket("T10", "2026-11-21 09:00:00", False, False),
        ]
        cp = checkpoint_of(tickets, SHIFTS, "A")
        self.assertEqual(cp["results"][0]["processed_ids"], ["T10", "T99"])

    def test_checkpoint_resume_matches_full_run(self):
        tickets, shifts = R.normalize(base_tickets(), SHIFTS)
        full = R.advance(tickets, shifts, "A", R.initial_checkpoint(tickets, shifts, "A"))
        partial = R.advance(tickets, shifts, "A", R.initial_checkpoint(tickets, shifts, "A"), shift_budget=1)
        self.assertEqual(partial["next_shift"], 1)
        resumed = R.advance(tickets, shifts, "A", partial)
        self.assertEqual(resumed, full)
        # 每个切分点都要一致。
        for cut in range(len(shifts) + 1):
            cp = R.advance(tickets, shifts, "A", R.initial_checkpoint(tickets, shifts, "A"), shift_budget=cut)
            cp = R.advance(tickets, shifts, "A", cp)
            self.assertEqual(cp, full)

    def test_unreached_and_early_arrivals(self):
        tickets = base_tickets() + [
            ticket("T06", "2026-11-21 16:00:00", False, False),   # 晚于末班
            ticket("T07", "2026-11-21 07:00:00", False, False),   # 早于首班
        ]
        result = R.simulate(tickets, SHIFTS, "A")
        self.assertEqual(result["summary"]["unreached"], 1)
        self.assertEqual(result["summary"]["early_arrivals"], 1)
        status = {row["ticket_id"]: row["status"] for row in result["tickets"]}
        self.assertEqual(status["T06"], R.UNREACHED)
        # 早到票并入首班，按票号参与排序，正常被处理。
        self.assertNotEqual(status["T07"], R.UNREACHED)
        self.assertEqual(result["shifts"][0]["new_arrivals"], 6)

    def test_pending_overdue_when_never_cleared(self):
        # 名额极少：S1 到 5 张失败票，每班只处理 1 张。
        tiny = [shift(s["shift_id"], s["start_at"], s["end_at"], 1, 1) for s in SHIFTS]
        result = R.simulate(base_tickets(), tiny, "A")
        status = {row["ticket_id"]: row["status"] for row in result["tickets"]}
        self.assertEqual(status["T04"], R.PENDING_OVERDUE)
        self.assertEqual(status["T05"], R.PENDING_OVERDUE)
        self.assertEqual(result["summary"]["pending_total"], 2)
        # T03 在 S2 末仍积压（deadline）→ 超时，T04、T05 随后超时；T02 在 S2 当班补掉不超时。
        self.assertEqual(result["summary"]["overdue_total"], 3)

    def test_validation_errors(self):
        with self.assertRaises(ValueError):
            R.simulate([], SHIFTS, "A")
        dup = base_tickets() + [ticket("T01", "2026-11-21 09:00:00", False, False)]
        with self.assertRaises(ValueError):
            R.simulate(dup, SHIFTS, "A")
        bad = [ticket("X", "2026-11-21 09:00:00", "no", False)]
        with self.assertRaises(ValueError):
            R.simulate(bad, SHIFTS, "A")
        with self.assertRaises(ValueError):
            R.simulate(base_tickets(), [shift("S1", "2026-11-21 10:00:00", "2026-11-21 09:00:00", 1, 1)], "A")
        with self.assertRaises(ValueError):
            R.simulate(base_tickets(), SHIFTS, "C")

    def test_arrival_index_boundaries(self):
        self.assertEqual(R.arrival_index(dt("2026-11-21 07:59:59"), SHIFTS_N), -1)
        self.assertEqual(R.arrival_index(dt("2026-11-21 08:00:00"), SHIFTS_N), 0)
        self.assertEqual(R.arrival_index(dt("2026-11-21 09:59:59"), SHIFTS_N), 0)
        self.assertEqual(R.arrival_index(dt("2026-11-21 10:00:00"), SHIFTS_N), 1)
        self.assertEqual(R.arrival_index(dt("2026-11-21 12:30:00"), SHIFTS_N), None)
        self.assertEqual(R.arrival_index(dt("2026-11-21 15:00:00"), SHIFTS_N), None)


class RecomputeTests(unittest.TestCase):
    def test_no_change_means_no_recompute(self):
        old_t, old_s = R.normalize(base_tickets(), SHIFTS)
        new_t, new_s = R.normalize([dict(row) for row in base_tickets()], [dict(row) for row in SHIFTS])
        self.assertEqual(R.recompute_boundary(old_t, old_s, new_t, new_s), len(new_s))

    def test_arrival_change_uses_earlier_of_old_and_new_shift(self):
        old_t, old_s = R.normalize(base_tickets(), SHIFTS)
        changed = [dict(row) for row in base_tickets()]
        # T01 从 S1 改到 S2 到票：旧班次 0、新班次 1，取较早者 0 才能保留有效前缀。
        changed[0] = ticket("T01", "2026-11-21 11:00:00", False, False)
        new_t, new_s = R.normalize(changed, SHIFTS)
        self.assertEqual(R.recompute_boundary(old_t, old_s, new_t, new_s), 0)

    def test_later_shift_only_change_keeps_earlier_prefix(self):
        # 在 S2 新增一张票：最早受影响班次为 1，S0 的结果必须原样保留。
        old_t, old_s = R.normalize(base_tickets(), SHIFTS)
        grown = base_tickets() + [ticket("T06", "2026-11-21 11:00:00", False, False)]
        new_t, new_s = R.normalize(grown, SHIFTS)
        self.assertEqual(R.recompute_boundary(old_t, old_s, new_t, new_s), 1)

    def test_delete_ticket_uses_old_arrival_shift(self):
        # 删除一张 S1 到票的票：从首班重算。
        old_t, old_s = R.normalize(base_tickets(), SHIFTS)
        shrunk = [row for row in base_tickets() if row["ticket_id"] != "T03"]
        new_t, new_s = R.normalize(shrunk, SHIFTS)
        self.assertEqual(R.recompute_boundary(old_t, old_s, new_t, new_s), 0)

    def test_quota_change_starts_at_that_shift(self):
        old_t, old_s = R.normalize(base_tickets(), SHIFTS)
        changed = [dict(row) for row in SHIFTS]
        changed[2] = shift("S3", "2026-11-21 13:00:00", "2026-11-21 15:00:00", 3, 2)
        new_t, new_s = R.normalize(base_tickets(), changed)
        self.assertEqual(R.recompute_boundary(old_t, old_s, new_t, new_s), 2)

    def test_structure_change_forces_full_recompute(self):
        old_t, old_s = R.normalize(base_tickets(), SHIFTS)
        new_t, new_s = R.normalize(base_tickets(), SHIFTS[:2])
        self.assertEqual(R.recompute_boundary(old_t, old_s, new_t, new_s), 0)

    def test_pass_flag_change_recomputes_arrival_shift(self):
        old_t, old_s = R.normalize(base_tickets(), SHIFTS)
        changed = [dict(row) for row in base_tickets()]
        # T01 原本 B 已通过，翻转为未通过：B 的首班失败池发生变化，从首班重算。
        changed[0] = ticket("T01", "2026-11-21 09:00:00", False, False)
        new_t, new_s = R.normalize(changed, SHIFTS)
        self.assertEqual(R.recompute_boundary(old_t, old_s, new_t, new_s), 0)

    def test_incremental_prefix_kept_bit_identical(self):
        # 在 S2 新增两张票：S1 之前各班排队构成不变，保留前缀位级一致；S2 起结果改变。
        old_t, old_s = R.normalize(base_tickets(), SHIFTS)
        old_a = R.advance(old_t, old_s, "A", R.initial_checkpoint(old_t, old_s, "A"))
        grown = base_tickets() + [
            ticket("T06", "2026-11-21 11:00:00", False, False),
            ticket("T07", "2026-11-21 11:30:00", False, False),
        ]
        new_t, new_s = R.normalize(grown, SHIFTS)
        boundary = R.recompute_boundary(old_t, old_s, new_t, new_s)
        self.assertEqual(boundary, 1)
        new_a = R.advance(new_t, new_s, "A", R.initial_checkpoint(new_t, new_s, "A"))
        for index in range(boundary):
            self.assertEqual(new_a["results"][index], old_a["results"][index])
        self.assertNotEqual(new_a["results"][boundary], old_a["results"][boundary])
        # 新票在 S2 之后进入队列，必然改变边界班次之后的积压。
        self.assertGreaterEqual(new_a["results"][-1]["backlog_end"], old_a["results"][-1]["backlog_end"])
        self.assertNotEqual(new_a["results"][boundary], old_a["results"][boundary])

    def test_iso_roundtrip(self):
        tickets, shifts = R.normalize(base_tickets(), SHIFTS)
        row_tickets, row_shifts = R.serializable(tickets, shifts)
        again_t, again_s = R.normalize(row_tickets, row_shifts)
        self.assertEqual(again_t, tickets)
        self.assertEqual(again_s, shifts)


class CompareTests(unittest.TestCase):
    def test_compare_and_retained_flags(self):
        result_a = R.simulate(base_tickets(), SHIFTS, "A")
        result_b = R.simulate(base_tickets(), SHIFTS, "B")
        merged = R.compare(result_a, result_b, boundary=1)
        self.assertTrue(merged[0]["retained"])
        self.assertFalse(merged[1]["retained"])
        self.assertEqual(len(merged), 3)
        totals = {
            "a_overdue": result_a["summary"]["overdue_total"],
            "b_overdue": result_b["summary"]["overdue_total"],
        }
        self.assertEqual(totals, {"a_overdue": 1, "b_overdue": 0})


if __name__ == "__main__":
    unittest.main()
