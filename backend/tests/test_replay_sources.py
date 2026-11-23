"""回放 CSV 解析与数据源版本逻辑的离线测试。

运行：python3 -I tests/test_replay_sources.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.domain import replay as R
from src.domain.replay_demo import replay_demo
from src.services import replay_sources as S


def csv_text(rows, fields):
    import csv
    import io
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def main():
    raw_tickets, raw_shifts = replay_demo()
    ticket_csv = csv_text(raw_tickets, S.TICKET_FIELDS)
    shift_csv = csv_text(raw_shifts, S.SHIFT_FIELDS)

    # 正常解析：布尔与中文表头、BOM 都能处理。
    tickets, shifts, warnings = S.parse_replay_rows("﻿" + ticket_csv, shift_csv)
    assert len(tickets) == len(raw_tickets)
    assert len(shifts) == len(raw_shifts)
    assert all(isinstance(row["a_pass"], bool) for row in tickets)
    assert warnings, "演示数据包含早到/晚到票，应产生预警"

    # 幂等：相同内容校验和一致。
    assert S.checksum(tickets, shifts) == S.checksum(*S.parse_replay_rows(ticket_csv, shift_csv)[:2])

    # 坏布尔值。
    bad_ticket = [dict(raw_tickets[0], a_pass="maybe")]
    try:
        S.parse_replay_rows(csv_text(bad_ticket, S.TICKET_FIELDS), shift_csv)
    except ValueError as error:
        assert "true/false" in str(error)
    else:
        raise AssertionError("坏布尔值应当被拒绝")

    # 坏名额（负数/非整数）。
    bad_shift = [dict(raw_shifts[0], quota_a="-3")]
    try:
        S.parse_replay_rows(ticket_csv, csv_text(bad_shift, S.SHIFT_FIELDS))
    except ValueError as error:
        assert "非负整数" in str(error)
    else:
        raise AssertionError("负名额应当被拒绝")

    # 表头不符。
    try:
        S.parse_replay_rows("ticket_id,arrived_at\nX,2026-11-21 08:00:00\n", shift_csv)
    except ValueError as error:
        assert "表头" in str(error)
    else:
        raise AssertionError("缺列表头应当被拒绝")

    # 票号重复。
    dup = [dict(raw_tickets[0]), dict(raw_tickets[0])]
    try:
        S.parse_replay_rows(csv_text(dup, S.TICKET_FIELDS), shift_csv)
    except ValueError as error:
        assert "重复" in str(error)
    else:
        raise AssertionError("重复票号应当被拒绝")

    # 模板可下载且能被自己的解析器读回。
    tpl_tickets = S.ticket_template().lstrip("﻿")
    tpl_shifts = S.shift_template().lstrip("﻿")
    parsed_t, parsed_s, _ = S.parse_replay_rows(tpl_tickets, tpl_shifts)
    assert parsed_t and parsed_s

    # 增量边界：把一张第二班到票的票改到第三班，旧班次 1、新班次 2，边界取较早者 1。
    old_t, old_s = R.normalize(tickets, shifts)
    second_batch_index = next(
        i for i, row in enumerate(tickets)
        if R.arrival_index(R.parse_dt(row["arrived_at"]), old_s) == 1)
    changed = [dict(row) for row in tickets]
    changed[second_batch_index] = {**changed[second_batch_index], "arrived_at": shifts[2]["start_at"]}
    new_t, new_s = R.normalize(changed, shifts)
    boundary = R.recompute_boundary(old_t, old_s, new_t, new_s)
    assert boundary == 1, boundary

    # 首班的票改期：旧班次为 0，边界必须是 0（全量重算首班）。
    changed_first = [dict(row) for row in tickets]
    changed_first[0] = {**changed_first[0], "arrived_at": shifts[1]["start_at"]}
    new_t2, new_s2 = R.normalize(changed_first, shifts)
    assert R.recompute_boundary(old_t, old_s, new_t2, new_s2) == 0

    print("sources OK: CSV 解析、校验、模板回读、指纹幂等、增量边界 全部通过")


if __name__ == "__main__":
    main()
