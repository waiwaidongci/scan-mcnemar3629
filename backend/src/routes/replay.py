import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.database import connection
from src.config.settings import ARTIFACTS
from src.constants.enums import Role, Status
from src.controllers.auth import allow, current
from src.domain import replay
from src.models.identity import User
from src.models.replay import ReplayRun, ReplaySource, ReplayTicketResult
from src.schemas.requests import ReplayRunCreate, ReplayUpload
from src.services import replay_sources
from src.services.audit import record
from src.utils.exports import write_replay_comparison
from src.utils.time import iso
from src.utils.wakeup import wake

router = APIRouter(prefix='/replay')

OVERDUE_STATUSES = (replay.DONE_OVERDUE, replay.PENDING_OVERDUE)


def _run_view(item):
    return {
        'id': item.id, 'code': item.code, 'engine': item.engine, 'status': item.status,
        'attempt': item.attempt,
        'error': item.error, 'published': item.published, 'stale_blocked': item.stale_blocked,
        'has_export': bool(item.export_file), 'export_file': item.export_file,
        'retained_shift_count': item.retained_shift_count,
        'summary': item.result['summary'] if item.result else None,
        'created_at': iso(item.created_at), 'finished_at': iso(item.finished_at),
    }


def _source_view(item):
    return {
        'id': item.id, 'name': item.name, 'filename': item.filename, 'version': item.version,
        'parent_id': item.parent_id, 'boundary': item.boundary,
        'ticket_count': item.ticket_count, 'shift_count': item.shift_count,
        'warnings': item.warnings, 'checksum': item.checksum,
        'author': item.author, 'created_at': iso(item.created_at),
    }


@router.get('/spec')
def replay_spec():
    # 仅返回字段与口径元数据，与公开的 /spec 一致，供登录前的工作台初始化使用。
    return {
        'ticket_fields': list(replay_sources.TICKET_FIELDS),
        'shift_fields': list(replay_sources.SHIFT_FIELDS),
        'engines': list(replay.ENGINES),
        'ticket_status': {
            replay.DONE: '已补录', replay.DONE_OVERDUE: '已补录（曾超时）',
            replay.PENDING: '排队中', replay.PENDING_OVERDUE: '超时未补',
            replay.UNREACHED: '晚于末班未入队',
        },
        'rules': [
            '仅引擎未通过的票据进入该引擎的待补队列，按到票时间升序处理，同刻按票号升序。',
            '当班名额用完即结转下一班，队列顺序保持不变。',
            '到票班次的下一班结束时仍未补录记为超时；超时后补录完成仍保留超时标记。',
            '到票记录更新后从最早受影响班次重算，更早班次结果原样保留。',
            '两套引擎都跑完并通过门禁后，验收页面与导出一次性切换；晚到的旧计算结果不允许发布。',
        ],
    }


@router.get('/template/tickets')
def ticket_template(actor: User = Depends(current)):
    return _csv_response(replay_sources.ticket_template(), '澄石补录到票记录模板.csv')


@router.get('/template/shifts')
def shift_template(actor: User = Depends(current)):
    return _csv_response(replay_sources.shift_template(), '澄石班次名额模板.csv')


def _csv_response(text, filename):
    from urllib.parse import quote
    from fastapi import Response
    return Response(text, media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(filename)}"})


@router.get('/sources')
def sources(db: Session = Depends(connection), actor: User = Depends(current)):
    items = db.scalars(select(ReplaySource).order_by(ReplaySource.version.desc()).limit(100))
    return [_source_view(item) for item in items]


@router.get('/sources/{source_id}')
def source_detail(source_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    item = db.get(ReplaySource, source_id)
    if not item:
        raise HTTPException(404, '找不到该回放数据源版本')
    return {**_source_view(item), 'tickets': item.tickets, 'shifts': item.shifts}


@router.post('/sources', status_code=201)
def upload_source(body: ReplayUpload, db: Session = Depends(connection),
                  actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    tickets, shifts, warnings = replay_sources.parse_replay_rows(body.ticket_csv, body.shift_csv)
    parent = None
    if body.parent_id is not None:
        parent = db.get(ReplaySource, body.parent_id)
        if not parent:
            raise HTTPException(404, '找不到所基于的旧数据源版本')
    else:
        parent = db.scalar(select(ReplaySource).order_by(ReplaySource.version.desc()).limit(1))
    boundary = None
    if parent is not None:
        old_tickets, old_shifts = replay.normalize(parent.tickets, parent.shifts)
        new_tickets, new_shifts = replay.normalize(tickets, shifts)
        boundary = replay.recompute_boundary(old_tickets, old_shifts, new_tickets, new_shifts)
    item, created = replay_sources.save_source(
        db, body.name, body.filename, tickets, shifts, warnings, parent, boundary, actor.username)
    if created:
        record(db, actor.username, 'replay_source.imported', item.id,
               version=item.version, boundary=boundary, tickets=item.ticket_count, shifts=item.shift_count)
    db.commit()
    return {**_source_view(item), 'created': created}


@router.post('/runs', status_code=202)
def start_run(body: ReplayRunCreate, db: Session = Depends(connection),
              actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    source = db.get(ReplaySource, body.source_id)
    if not source:
        raise HTTPException(404, '找不到指定的回放数据源版本')
    code = f'RR-{uuid.uuid4().hex[:12]}'
    for engine in replay.ENGINES:
        db.add(ReplayRun(code=code, source_id=source.id, engine=engine,
                         name=body.name, author=actor.username))
    db.flush()
    record(db, actor.username, 'replay.queued', code, source_id=source.id, engines=list(replay.ENGINES))
    db.commit()
    wake()
    return run_group(db, code)


def run_group(db, code):
    items = list(db.scalars(select(ReplayRun).where(ReplayRun.code == code).order_by(ReplayRun.engine)))
    if not items:
        raise HTTPException(404, '找不到该回放运行')
    source = db.get(ReplaySource, items[0].source_id)
    engines = {item.engine: _run_view(item) for item in items}
    merged = None
    if all(item.status == Status.COMPLETE and item.result for item in items):
        by_engine = {item.engine: item for item in items}
        merged = replay.compare(by_engine['A'].result, by_engine['B'].result, source.boundary)
    return {
        'code': code, 'name': f'{source.name} · v{source.version}',
        'source': _source_view(source), 'published': all(item.published for item in items),
        'stale_blocked': any(item.stale_blocked for item in items),
        'all_complete': all(item.status == Status.COMPLETE for item in items),
        'engines': engines, 'merged': merged,
        'created_at': iso(items[0].created_at),
    }


@router.get('/runs')
def list_runs(db: Session = Depends(connection), actor: User = Depends(current)):
    rows = db.scalars(select(ReplayRun).order_by(ReplayRun.id.desc()).limit(200)).all()
    codes = list(dict.fromkeys(item.code for item in rows))
    return [run_group(db, code) for code in codes[:50]]


@router.get('/runs/{code}')
def run_detail(code: str, db: Session = Depends(connection), actor: User = Depends(current)):
    return run_group(db, code)


@router.get('/runs/{code}/tickets')
def run_tickets(code: str, engine: str = Query(pattern='^(A|B)$'),
                status_filter: str = Query('all', alias='status'),
                offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                db: Session = Depends(connection), actor: User = Depends(current)):
    run = db.scalar(select(ReplayRun).where(ReplayRun.code == code, ReplayRun.engine == engine))
    if not run:
        raise HTTPException(404, '找不到该引擎的回放运行')
    query = select(ReplayTicketResult).where(ReplayTicketResult.run_id == run.id)
    if status_filter == 'overdue':
        query = query.where(ReplayTicketResult.status.in_(OVERDUE_STATUSES))
    elif status_filter != 'all':
        if status_filter not in (replay.DONE, replay.DONE_OVERDUE, replay.PENDING,
                                 replay.PENDING_OVERDUE, replay.UNREACHED):
            raise HTTPException(422, '不支持的票据状态筛选')
        query = query.where(ReplayTicketResult.status == status_filter)
    total = len(db.scalars(query).all())
    rows = db.scalars(query.order_by(ReplayTicketResult.arrived_at, ReplayTicketResult.ticket_id)
                      .offset(offset).limit(limit)).all()
    return {'total': total, 'rows': [{
        'ticket_id': row.ticket_id, 'arrived_at': row.arrived_at, 'arrival_shift': row.arrival_shift,
        'engine': row.engine, 'status': row.status, 'processed_shift': row.processed_shift,
    } for row in rows]}


def _require_published(db, code):
    items = list(db.scalars(select(ReplayRun).where(ReplayRun.code == code)))
    if not items:
        raise HTTPException(404, '找不到该回放运行')
    if not all(item.published and item.export_file for item in items):
        raise HTTPException(409, '两套引擎尚未全部算完并通过门禁，暂不能导出或切换验收页面')
    if any(item.stale_blocked for item in items):
        raise HTTPException(409, '存在晚到的旧计算结果，已拦截发布，请重新发起回放')
    return items


@router.get('/runs/engine/{run_id}/export')
def engine_export(run_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    run = db.get(ReplayRun, run_id)
    if not run:
        raise HTTPException(404, '找不到该引擎的回放运行')
    items = _require_published(db, run.code)
    item = next((row for row in items if row.id == run_id), run)
    path = ARTIFACTS / item.export_file
    if not path.is_file():
        raise HTTPException(410, '导出文件已不可用，请重新运行')
    return FileResponse(path, media_type='text/csv',
                        filename=f'replay-engine-{item.engine}-{item.code}.csv')


@router.get('/runs/{code}/comparison')
def comparison_export(code: str, db: Session = Depends(connection), actor: User = Depends(current)):
    items = _require_published(db, code)
    by_engine = {item.engine: item for item in items}
    merged = replay.compare(by_engine['A'].result, by_engine['B'].result,
                            db.get(ReplaySource, items[0].source_id).boundary)
    name = write_replay_comparison(code, merged,
                                   by_engine['A'].result['summary'], by_engine['B'].result['summary'])
    path = ARTIFACTS / name
    return FileResponse(path, media_type='text/csv', filename=f'replay-comparison-{code}.csv')
