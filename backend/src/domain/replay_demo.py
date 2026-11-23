"""补录回放的确定性演示数据：覆盖积压、结转、超时与晚到票。"""

from datetime import datetime, timedelta


def _str(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def replay_demo():
    """返回 (到票记录行, 班次名额行)，时间为 2026-11-21 当天。"""
    base = datetime(2026, 11, 21, 8, 0, 0)
    shifts = []
    # 四个班次，每班 2 小时；A 名额偏紧，B 名额略宽。
    shift_specs = [
        ("SHIFT-1121-1", 0, 2, 4),
        ("SHIFT-1121-2", 2, 2, 4),
        ("SHIFT-1121-3", 5, 4, 5),
        ("SHIFT-1121-4", 7, 4, 5),
    ]
    for shift_id, offset_hours, quota_a, quota_b in shift_specs:
        start = base + timedelta(hours=offset_hours)
        shifts.append({
            "shift_id": shift_id,
            "start_at": _str(start),
            "end_at": _str(start + timedelta(hours=2)),
            "quota_a": quota_a,
            "quota_b": quota_b,
        })

    tickets = []
    # 24 张票分四批到票，A 失败 14 张、B 失败 8 张，制造 B 积压更少的对比。
    plan = [
        # (到票偏移分钟, 数量, A 失败数, B 失败数)
        (15, 6, 5, 3),
        (135, 6, 4, 2),
        (315, 6, 3, 2),
        (435, 6, 2, 1),
    ]
    serial = 1
    for offset_minutes, count, a_fail, b_fail in plan:
        arrived = base + timedelta(minutes=offset_minutes)
        for index in range(count):
            # 同刻到达靠票号区分：失败的排前面，便于观察处理顺序。
            a_pass = index >= a_fail
            b_pass = index >= b_fail
            tickets.append({
                "ticket_id": f"BL-20261121-{serial:03d}",
                "arrived_at": _str(arrived),
                "a_pass": a_pass,
                "b_pass": b_pass,
            })
            serial += 1
    # 两张边界票：一张早于首班、一张晚于末班，用于预警展示。
    tickets.append({
        "ticket_id": "BL-20261121-E01",
        "arrived_at": _str(base - timedelta(minutes=40)),
        "a_pass": False,
        "b_pass": False,
    })
    tickets.append({
        "ticket_id": "BL-20261121-L01",
        "arrived_at": _str(base + timedelta(hours=9, minutes=30)),
        "a_pass": False,
        "b_pass": True,
    })
    return tickets, shifts
