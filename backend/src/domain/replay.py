"""补录回放核心引擎（仅依赖标准库，便于离线单测）。

业务口径
--------
同一份到票记录与班次名额，分别交给现用引擎 A 与候选引擎 B 回放：

- 只有引擎未通过的票据才进入该引擎的待补队列。
- 待补票据按到票时间升序处理，同一时刻按票号升序。
- 当班补不完的名额结转到下一班，队列顺序保持不变。
- 票据在到票班次的下一班结束时仍未补录，即记为超时；超时票据继续留在队列，
  后续班次仍可补录，补录完成时保留超时标记。
- 到票记录更新后，从最早受影响的班次起重算，更早班次的结果原样保留。
- 计算可在任意班次之间中断，凭检查点续跑，续跑结果须与一次跑完完全一致。

所有时间在入口规范化为朴素 UTC ``datetime``，模块内部不读取墙上时钟，
同样的输入必然得到同样的输出。
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import UTC, datetime

ENGINES = ("A", "B")
PASS_KEY = {"A": "a_pass", "B": "b_pass"}
QUOTA_KEY = {"A": "quota_a", "B": "quota_b"}

# 单张票据的终局状态。
DONE = "done"                    # 已补录，未发生超时
DONE_OVERDUE = "done_overdue"    # 已补录，但曾超过 SLA
PENDING_OVERDUE = "pending_overdue"  # 已超时且尚未补录
PENDING = "pending"              # 仍在 SLA 内等待补录
UNREACHED = "unreached"          # 到票晚于最后一班，队列从未覆盖到


def parse_dt(raw):
    """把 CSV/JSON 中的时间字符串规范化为朴素 UTC 时间。"""
    if isinstance(raw, datetime):
        dt = raw
    else:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("时间格式有误，应为 YYYY-MM-DD HH:MM:SS")
        text = raw.strip().replace(" ", "T")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"时间格式有误：{raw}") from None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def _norm_tickets(raw_rows):
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("到票记录至少需要一条票据。")
    tickets = []
    seen = set()
    for index, row in enumerate(raw_rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条到票记录格式有误。")
        ticket_id = row.get("ticket_id")
        if not isinstance(ticket_id, str) or not ticket_id.strip() or len(ticket_id.strip()) > 64:
            raise ValueError(f"第 {index} 条票据编号格式有误。")
        ticket_id = ticket_id.strip()
        if ticket_id in seen:
            raise ValueError(f"票据编号重复：{ticket_id}。")
        seen.add(ticket_id)
        record = {"ticket_id": ticket_id, "arrived_at": parse_dt(row.get("arrived_at"))}
        for engine in ENGINES:
            key = PASS_KEY[engine]
            value = row.get(key)
            if not isinstance(value, bool):
                raise ValueError(f"第 {index} 条票据的引擎 {engine} 通过判定须为布尔值。")
            record[key] = value
        tickets.append(record)
    tickets.sort(key=lambda item: (item["arrived_at"], item["ticket_id"]))
    return tickets


def _norm_shifts(raw_rows):
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("班次名额至少需要一个班次。")
    shifts = []
    seen = set()
    for index, row in enumerate(raw_rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条班次记录格式有误。")
        shift_id = row.get("shift_id")
        if not isinstance(shift_id, str) or not shift_id.strip() or len(shift_id.strip()) > 64:
            raise ValueError(f"第 {index} 条班次编号格式有误。")
        shift_id = shift_id.strip()
        if shift_id in seen:
            raise ValueError(f"班次编号重复：{shift_id}。")
        seen.add(shift_id)
        start_at = parse_dt(row.get("start_at"))
        end_at = parse_dt(row.get("end_at"))
        if not start_at < end_at:
            raise ValueError(f"班次 {shift_id} 的开始时间须早于结束时间。")
        shift = {"shift_id": shift_id, "start_at": start_at, "end_at": end_at}
        for engine in ENGINES:
            quota = row.get(QUOTA_KEY[engine])
            if isinstance(quota, bool) or not isinstance(quota, int) or quota < 0:
                raise ValueError(f"班次 {shift_id} 的引擎 {engine} 名额须为非负整数。")
            shift[QUOTA_KEY[engine]] = quota
        shifts.append(shift)
    shifts.sort(key=lambda item: item["start_at"])
    for previous, current in zip(shifts, shifts[1:]):
        if current["start_at"] < previous["end_at"]:
            raise ValueError(f"班次 {previous['shift_id']} 与 {current['shift_id']} 时间重叠，请先修正班次表。")
    return shifts


def normalize(raw_tickets, raw_shifts):
    """校验并规范化输入，返回可安全持久化的结构。"""
    tickets = _norm_tickets(raw_tickets)
    shifts = _norm_shifts(raw_shifts)
    return tickets, shifts


def serializable(tickets, shifts):
    """把规范化后的 datetime 结构转成可入库的 JSON 结构。"""
    ticket_rows = [
        {"ticket_id": item["ticket_id"], "arrived_at": iso(item["arrived_at"]),
         "a_pass": item["a_pass"], "b_pass": item["b_pass"]}
        for item in tickets
    ]
    shift_rows = [
        {"shift_id": item["shift_id"], "start_at": iso(item["start_at"]),
         "end_at": iso(item["end_at"]), "quota_a": item["quota_a"], "quota_b": item["quota_b"]}
        for item in shifts
    ]
    return ticket_rows, shift_rows


def _starts(shifts):
    return [item["start_at"] for item in shifts]


def arrival_index(arrived_at, shifts):
    """返回到票班次下标。

    - 落在某班次 [start, end) 内返回该班次下标；
    - 早于首个班次返回 -1（并入首班到票，另在摘要里预警）；
    - 晚于末班结束返回 None（队列永远覆盖不到）。
    """
    starts = _starts(shifts)
    pos = bisect_left(starts, arrived_at)
    if pos == 0:
        return -1 if arrived_at < shifts[0]["start_at"] else 0
    if pos == len(shifts) or arrived_at < shifts[pos]["start_at"]:
        candidate = pos - 1
        if arrived_at < shifts[candidate]["end_at"]:
            return candidate
        return None
    return pos


def initial_checkpoint(tickets, shifts, engine):
    """构造空检查点。pending 只存票号，续跑时按编号回查票据。"""
    meta = {}
    early = 0
    unreached = 0
    for item in tickets:
        index = arrival_index(item["arrived_at"], shifts)
        if index == -1:
            early += 1
        if index is None:
            unreached += 1
        meta[item["ticket_id"]] = index
    return {
        "engine": engine,
        "next_shift": 0,
        "pending": [],
        "overdue": [],
        "results": [],
        "ticket_meta": meta,
        "early_arrivals": early,
        "unreached": unreached,
    }


def advance(tickets, shifts, engine, checkpoint, shift_budget=None):
    """从检查点继续回放，最多再处理 ``shift_budget`` 个班次。

    不传预算时一次跑完全部班次。返回新的检查点（不修改入参）。
    """
    state = {
        "engine": engine,
        "next_shift": checkpoint["next_shift"],
        "pending": list(checkpoint["pending"]),
        "overdue": list(checkpoint["overdue"]),
        "results": list(checkpoint["results"]),
        "ticket_meta": dict(checkpoint["ticket_meta"]),
        "early_arrivals": checkpoint.get("early_arrivals", 0),
        "unreached": checkpoint.get("unreached", 0),
    }
    pass_key = PASS_KEY[engine]
    # 失败票据已在规范化时按（到票时间, 票号）排好序。
    failed = [item for item in tickets if not item[pass_key]]
    failed_ids = [item["ticket_id"] for item in failed]
    started = state["next_shift"]
    remaining = shift_budget if shift_budget is not None else len(shifts) - started
    while state["next_shift"] < len(shifts) and remaining > 0:
        si = state["next_shift"]
        shift = shifts[si]
        new_arrivals = 0
        for ticket_id in failed_ids:
            index = state["ticket_meta"][ticket_id]
            arrival = 0 if index == -1 else index
            if arrival == si:
                state["pending"].append(ticket_id)
                new_arrivals += 1
        quota = shift[QUOTA_KEY[engine]]
        processed_ids = state["pending"][:quota]
        state["pending"] = state["pending"][quota:]
        # SLA 在次班「结束」时判定：先处理名额，再对仍积压的票打超时标记，
        # 当班补录完成的票不计超时。到票班次（早到者按首班计）的下一班为 deadline。
        for ticket_id in state["pending"]:
            index = state["ticket_meta"][ticket_id]
            arrival = 0 if index == -1 else index
            if arrival + 1 == si and ticket_id not in state["overdue"]:
                state["overdue"].append(ticket_id)
        overdue_open = sum(1 for ticket_id in state["pending"] if ticket_id in state["overdue"])
        state["results"].append({
            "shift_id": shift["shift_id"],
            "start_at": iso(shift["start_at"]),
            "end_at": iso(shift["end_at"]),
            "quota": quota,
            "new_arrivals": new_arrivals,
            "processed": len(processed_ids),
            "backlog_end": len(state["pending"]),
            "overdue_open": overdue_open,
            "overdue_cumulative": len(state["overdue"]),
            "processed_ids": processed_ids,
        })
        state["next_shift"] = si + 1
        remaining -= 1
    return state


def _ticket_records(tickets, shifts, engine, checkpoint):
    pass_key = PASS_KEY[engine]
    processed_shift = {}
    processed_overdue = set()
    for index, row in enumerate(checkpoint["results"]):
        for ticket_id in row["processed_ids"]:
            processed_shift[ticket_id] = index
            if ticket_id in checkpoint["overdue"]:
                processed_overdue.add(ticket_id)
    records = []
    for item in tickets:
        if item[pass_key]:
            continue
        ticket_id = item["ticket_id"]
        index = checkpoint["ticket_meta"][ticket_id]
        if ticket_id in processed_shift:
            status = DONE_OVERDUE if ticket_id in processed_overdue else DONE
            done_shift = shifts[processed_shift[ticket_id]]["shift_id"]
        elif index is None:
            status = UNREACHED
            done_shift = None
        elif ticket_id in checkpoint["overdue"]:
            status = PENDING_OVERDUE
            done_shift = None
        else:
            status = PENDING
            done_shift = None
        records.append({
            "ticket_id": ticket_id,
            "arrived_at": iso(item["arrived_at"]),
            "arrival_shift": shifts[index]["shift_id"] if index is not None and index >= 0 else ("早于首班" if index == -1 else "晚于末班"),
            "engine": engine,
            "status": status,
            "processed_shift": done_shift,
        })
    records.sort(key=lambda item: (item["arrived_at"], item["ticket_id"]))
    return records


def finalize(tickets, shifts, engine, checkpoint):
    """检查点跑完后组装对外结果；未跑完时抛出 ValueError。"""
    if checkpoint["next_shift"] < len(shifts):
        raise ValueError("回放尚未跑完，无法汇总。")
    rows = [
        {key: value for key, value in row.items() if key != "processed_ids"}
        for row in checkpoint["results"]
    ]
    records = _ticket_records(tickets, shifts, engine, checkpoint)
    processed_total = sum(row["processed"] for row in rows)
    failed_total = sum(1 for item in tickets if not item[PASS_KEY[engine]])
    overdue_total = len(checkpoint["overdue"])
    pending_total = len(checkpoint["pending"])
    return {
        "engine": engine,
        "summary": {
            "failed_total": failed_total,
            "processed_total": processed_total,
            "overdue_total": overdue_total,
            "pending_total": pending_total,
            "unreached": checkpoint.get("unreached", 0),
            "early_arrivals": checkpoint.get("early_arrivals", 0),
        },
        "shifts": rows,
        "tickets": records,
    }


def simulate(raw_tickets, raw_shifts, engine):
    """一次性完整回放的便捷入口。"""
    if engine not in ENGINES:
        raise ValueError("引擎编号只能是 A 或 B。")
    tickets, shifts = normalize(raw_tickets, raw_shifts)
    checkpoint = advance(tickets, shifts, engine, initial_checkpoint(tickets, shifts, engine))
    return finalize(tickets, shifts, engine, checkpoint)


def _shift_of(ticket, shifts):
    """票据参与排队的班次；晚于末班返回末位（保守处理，触发全量）。"""
    index = arrival_index(ticket["arrived_at"], shifts)
    if index is None:
        return len(shifts)
    return max(0, index)


def recompute_boundary(old_tickets, old_shifts, new_tickets, new_shifts):
    """计算最早受影响班次下标。

    - 班次结构（数量、编号、时间窗、顺序）变化 → 从第 0 班重算；
    - 某班名额变化 → 从该班重算；
    - 新增/删除/改期/改判定的票据 → 取相关票据旧、新到票班次的较早者重算，
      确保边界之前各班的排队构成完全不变，结果可原样保留；
    - 没有任何差异时返回班次总数，表示无需重算。
    """
    if len(old_shifts) != len(new_shifts):
        return 0
    for old, new in zip(old_shifts, new_shifts):
        if (old["shift_id"] != new["shift_id"] or old["start_at"] != new["start_at"]
                or old["end_at"] != new["end_at"]):
            return 0
    boundary = len(new_shifts)
    for index, (old, new) in enumerate(zip(old_shifts, new_shifts)):
        if any(old[QUOTA_KEY[engine]] != new[QUOTA_KEY[engine]] for engine in ENGINES):
            boundary = min(boundary, index)
    old_map = {item["ticket_id"]: item for item in old_tickets}
    new_map = {item["ticket_id"]: item for item in new_tickets}
    for ticket_id, new_item in new_map.items():
        old_item = old_map.get(ticket_id)
        if old_item is None:
            # 新增票：从它的到票班次起重算，更早班次不受影响。
            boundary = min(boundary, _shift_of(new_item, new_shifts))
            continue
        if (old_item["arrived_at"] != new_item["arrived_at"]
                or old_item["a_pass"] != new_item["a_pass"]
                or old_item["b_pass"] != new_item["b_pass"]):
            affected = min(_shift_of(old_item, old_shifts), _shift_of(new_item, new_shifts))
            boundary = min(boundary, affected)
    for old_item in old_tickets:
        if old_item["ticket_id"] not in new_map:
            boundary = min(boundary, _shift_of(old_item, old_shifts))
    return boundary


def compare(results_a, results_b, boundary=None):
    """合并两套引擎的各班结果，供验收页与导出使用。"""
    by_shift_a = {row["shift_id"]: row for row in results_a["shifts"]}
    by_shift_b = {row["shift_id"]: row for row in results_b["shifts"]}
    merged = []
    for index, row_a in enumerate(results_a["shifts"]):
        row_b = by_shift_b[row_a["shift_id"]]
        merged.append({
            "shift_id": row_a["shift_id"],
            "start_at": row_a["start_at"],
            "end_at": row_a["end_at"],
            "retained": boundary is not None and index < boundary,
            "quota_a": row_a["quota"],
            "quota_b": row_b["quota"],
            "backlog_a": row_a["backlog_end"],
            "backlog_b": row_b["backlog_end"],
            "backlog_delta": row_b["backlog_end"] - row_a["backlog_end"],
            "overdue_a": row_a["overdue_cumulative"],
            "overdue_b": row_b["overdue_cumulative"],
            "processed_a": row_a["processed"],
            "processed_b": row_b["processed"],
        })
    return merged
