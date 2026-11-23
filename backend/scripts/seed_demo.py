import json
from sqlalchemy import func, select
from src.config.database import Session
from src.constants.enums import Role
from src.domain.demo import demo_rows
from src.domain.replay_demo import replay_demo
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.replay import ReplaySource
from src.models.source import Dataset
from src.services.datasets import save
from src.services.replay_sources import checksum, parse_replay_rows
from src.utils.security import hash_password


def _seed_replay_source(db):
    raw_tickets, raw_shifts = replay_demo()
    import csv
    import io

    def to_csv(rows, fields):
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue()

    ticket_csv = to_csv(raw_tickets, ("ticket_id", "arrived_at", "a_pass", "b_pass"))
    shift_csv = to_csv(raw_shifts, ("shift_id", "start_at", "end_at", "quota_a", "quota_b"))
    tickets, shifts, warnings = parse_replay_rows(ticket_csv, shift_csv)
    signature = checksum(tickets, shifts)
    existing = db.scalar(select(ReplaySource).where(ReplaySource.checksum == signature))
    if existing:
        return existing, False
    source = ReplaySource(
        name="澄石零售 11 月 21 日补录回放样本", filename="澄石补录回放样本_20261121.csv",
        checksum=signature, version=1, parent_id=None, boundary=None,
        tickets=tickets, shifts=shifts, ticket_count=len(tickets), shift_count=len(shifts),
        warnings=warnings, author="seed",
    )
    db.add(source)
    db.flush()
    return source, True


def seed():
    with Session.begin() as db:
        for role in Role:
            if not db.get(User, role.value):
                db.add(User(username=role.value, role=role, password_hash=hash_password(f'{role.value}-demo-2611')))
        item, created = save(db, SPEC['default_dataset'], SPEC['sample_name'], demo_rows(), 'seed')
        replay_item, replay_created = _seed_replay_source(db)
        return {'users': db.scalar(select(func.count()).select_from(User)),
                'datasets': db.scalar(select(func.count()).select_from(Dataset)),
                'seed_dataset_id': item.id, 'row_count': item.row_count, 'checksum': item.checksum,
                'created_dataset': created,
                'replay_source_id': replay_item.id, 'replay_created': replay_created,
                'replay_tickets': replay_item.ticket_count, 'replay_shifts': replay_item.shift_count}


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))
