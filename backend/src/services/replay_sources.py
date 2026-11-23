"""补录回放数据源的 CSV 解析与版本管理。"""

import csv
import hashlib
import io
import json

from src.domain import replay

TICKET_FIELDS = ("ticket_id", "arrived_at", "a_pass", "b_pass")
SHIFT_FIELDS = ("shift_id", "start_at", "end_at", "quota_a", "quota_b")
BOOL_TEXT = {"true": True, "false": False, "1": True, "0": False, "是": True, "否": False}


def _read_csv(text, expected, label):
    reader = csv.DictReader(io.StringIO(text.removeprefix("\ufeff"), newline=""), strict=True)
    headers = reader.fieldnames or []
    if not set(expected) <= set(headers) or len(headers) != len(set(headers)):
        raise ValueError(f"{label} CSV 表头与字段说明不符，请下载模板检查列名。")
    extra = set(headers) - set(expected)
    if extra:
        raise ValueError(f"{label} CSV 含未知列：{'、'.join(sorted(extra))}。")
    rows = []
    for raw in reader:
        if None in raw or any(value is None for value in raw.values()):
            raise ValueError(f"{label} CSV 第 {reader.line_num} 行的列数与表头不一致。")
        rows.append({key: (raw.get(key) or "").strip() for key in expected})
    if not rows:
        raise ValueError(f"{label} CSV 至少需要一行有效数据。")
    if len(rows) > 5000:
        raise ValueError(f"{label} CSV 最多 5000 行。")
    return rows


def _as_bool(raw, line, label):
    try:
        return BOOL_TEXT[raw.lower()]
    except KeyError:
        raise ValueError(f"第 {line} 行的{label}须为 true/false（或 是/否）。") from None


def _as_int(raw, line, label):
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"第 {line} 行的{label}须为非负整数。") from None
    if value < 0:
        raise ValueError(f"第 {line} 行的{label}须为非负整数。")
    return value


def parse_replay_rows(ticket_text, shift_text):
    """解析两份 CSV，返回规范化后的 (tickets, shifts, warnings)。"""
    raw_tickets = _read_csv(ticket_text, TICKET_FIELDS, "到票记录")
    raw_shifts = _read_csv(shift_text, SHIFT_FIELDS, "班次名额")

    ticket_rows = []
    for index, row in enumerate(raw_tickets, 2):
        ticket_rows.append({
            "ticket_id": row["ticket_id"],
            "arrived_at": row["arrived_at"],
            "a_pass": _as_bool(row["a_pass"], index, "引擎 A 通过判定"),
            "b_pass": _as_bool(row["b_pass"], index, "引擎 B 通过判定"),
        })
    shift_rows = []
    for index, row in enumerate(raw_shifts, 2):
        shift_rows.append({
            "shift_id": row["shift_id"],
            "start_at": row["start_at"],
            "end_at": row["end_at"],
            "quota_a": _as_int(row["quota_a"], index, "引擎 A 名额"),
            "quota_b": _as_int(row["quota_b"], index, "引擎 B 名额"),
        })

    tickets, shifts = replay.normalize(ticket_rows, shift_rows)
    stored_tickets, stored_shifts = replay.serializable(tickets, shifts)
    warnings = []
    early = sum(1 for item in tickets if replay.arrival_index(item["arrived_at"], shifts) == -1)
    unreached = sum(1 for item in tickets if replay.arrival_index(item["arrived_at"], shifts) is None)
    if early:
        warnings.append(f"有 {early} 张票早于首个班次开始，已并入首班排队。")
    if unreached:
        warnings.append(f"有 {unreached} 张票晚于末班结束，永远排不进队列，需确认班次安排。")
    return stored_tickets, stored_shifts, warnings


def checksum(tickets, shifts):
    raw = json.dumps({"tickets": tickets, "shifts": shifts}, ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def save_source(db, name, filename, tickets, shifts, warnings, parent, boundary, actor):
    from sqlalchemy import select
    from src.models.replay import ReplaySource

    signature = checksum(tickets, shifts)
    existing = db.scalar(select(ReplaySource).where(ReplaySource.checksum == signature).with_for_update())
    if existing:
        return existing, False
    latest = db.scalar(select(ReplaySource).order_by(ReplaySource.version.desc()).limit(1))
    version = (latest.version + 1) if latest else 1
    source = ReplaySource(
        name=name, filename=filename, checksum=signature, version=version,
        parent_id=parent.id if parent else None, boundary=boundary,
        tickets=tickets, shifts=shifts, ticket_count=len(tickets), shift_count=len(shifts),
        warnings=warnings, author=actor,
    )
    try:
        from sqlalchemy.exc import IntegrityError
        with db.begin_nested():
            db.add(source)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(ReplaySource).where(ReplaySource.checksum == signature).with_for_update())
        if existing:
            return existing, False
        raise
    return source, True


def ticket_template():
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(TICKET_FIELDS)
    writer.writerow(["BL-20261121-001", "2026-11-21 08:20:00", "false", "true"])
    return "\ufeff" + buffer.getvalue()


def shift_template():
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(SHIFT_FIELDS)
    writer.writerow(["SHIFT-20261121-1", "2026-11-21 08:00:00", "2026-11-21 10:00:00", "20", "25"])
    return "\ufeff" + buffer.getvalue()
