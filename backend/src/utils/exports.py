import csv
from src.config.settings import ARTIFACTS


def safe_cell(value):
    if isinstance(value, str) and (value.lstrip().startswith(('=', '+', '-', '@'))
                                   or value.startswith(('\t', '\r', '\n'))):
        return "'" + value
    return value


def _write_csv(path, rows):
    staging = path.with_suffix('.tmp')
    with staging.open('w', encoding='utf-8-sig', newline='') as output:
        writer = csv.writer(output)
        writer.writerows([[safe_cell(cell) for cell in row] for row in rows])
    staging.replace(path)


def write_report(run_id, attempt, result):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    name = f'analysis-{run_id}-{attempt}.csv'
    path = ARTIFACTS / name
    rows = []
    rows.append([result['headline']])
    rows.append([result.get('note', '')])
    for metric in result['metrics']:
        rows.append([metric['label'], metric['value'], metric.get('detail', '')])
    for table in result['tables']:
        rows.append([])
        rows.append([table['title']])
        columns = table['columns']
        rows.append([column['label'] for column in columns])
        rows.extend([row.get(column['key']) for column in columns] for row in table['rows'])
    _write_csv(path, rows)
    return name


def write_replay_report(run_id, attempt, engine, result):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    name = f'replay-{run_id}-{attempt}.csv'
    path = ARTIFACTS / name
    summary = result['summary']
    rows = [
        [f'补录回放 · 引擎 {engine}'],
        ['失败票据', summary['failed_total'], '需补录的票据总数'],
        ['已补录', summary['processed_total']],
        ['超时票据（累计）', summary['overdue_total'], '次班结束仍未补录即计超时'],
        ['仍在排队', summary['pending_total']],
        ['晚于末班未入队', summary['unreached']],
        [],
        ['班次', '开始', '结束', '名额', '本班到票', '本班补录', '班末积压', '其中超时未补', '累计超时'],
    ]
    for row in result['shifts']:
        rows.append([row['shift_id'], row['start_at'], row['end_at'], row['quota'],
                     row['new_arrivals'], row['processed'], row['backlog_end'],
                     row['overdue_open'], row['overdue_cumulative']])
    rows.append([])
    rows.append(['票据编号', '到票时间', '到票班次', '状态', '补录班次'])
    status_text = {
        'done': '已补录', 'done_overdue': '已补录（曾超时）',
        'pending': '排队中', 'pending_overdue': '超时未补', 'unreached': '晚于末班未入队',
    }
    for row in result['tickets']:
        rows.append([row['ticket_id'], row['arrived_at'], row['arrival_shift'],
                     status_text.get(row['status'], row['status']), row['processed_shift'] or ''])
    _write_csv(path, rows)
    return name


def write_replay_comparison(code, merged, summary_a, summary_b):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    name = f'replay-compare-{code}.csv'
    path = ARTIFACTS / name
    rows = [
        [f'补录回放双引擎对比 · {code}'],
        ['指标', '现用引擎 A', '候选引擎 B', 'B−A'],
        ['失败票据', summary_a['failed_total'], summary_b['failed_total'],
         summary_b['failed_total'] - summary_a['failed_total']],
        ['已补录', summary_a['processed_total'], summary_b['processed_total'],
         summary_b['processed_total'] - summary_a['processed_total']],
        ['超时票据（累计）', summary_a['overdue_total'], summary_b['overdue_total'],
         summary_b['overdue_total'] - summary_a['overdue_total']],
        ['仍在排队', summary_a['pending_total'], summary_b['pending_total'],
         summary_b['pending_total'] - summary_a['pending_total']],
        [],
        ['班次', '开始', '结束', 'A 名额', 'B 名额', 'A 班末积压', 'B 班末积压', '积压差(B−A)',
         'A 累计超时', 'B 累计超时', '结果来源'],
    ]
    for row in merged:
        rows.append([row['shift_id'], row['start_at'], row['end_at'], row['quota_a'], row['quota_b'],
                     row['backlog_a'], row['backlog_b'], row['backlog_delta'],
                     row['overdue_a'], row['overdue_b'], '保留旧结果' if row['retained'] else '重算结果'])
    _write_csv(path, rows)
    return name
