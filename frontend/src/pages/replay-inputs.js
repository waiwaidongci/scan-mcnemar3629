import {feedback} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

function fileField(key, label) {
  return el('label', {class:'field', for:key}, el('span', {}, label),
    el('input', {id:key, type:'file', name:key, accept:'.csv,text/csv', required:true}));
}

export async function replayInputs(route, refresh) {
  const {replaySources, replaySpec} = workspace;
  const selected = replaySources.find(item => item.id === route.id) || replaySources[0];
  const error = feedback();
  const ticketSample = button('下载到票记录模板', {class:'quiet',
    onClick:event => handler(ticketSample, error, () =>
      workspace.download('/replay/template/tickets', '澄石补录到票记录模板.csv'))(event)});
  const shiftSample = button('下载班次名额模板', {class:'quiet',
    onClick:event => handler(shiftSample, error, () =>
      workspace.download('/replay/template/shifts', '澄石班次名额模板.csv'))(event)});

  const detail = selected && await workspace.replaySource(selected.id);
  const preview = detail ? panel(`数据源 v${detail.version}`,
    el('div', {class:'actions'},
      el('label', {for:'replay-source-id', class:'sr'}, '选择数据源版本'),
      el('select', {id:'replay-source-id', 'aria-label':'选择数据源版本',
        onChange:event => { location.hash = `#replay-inputs/${event.target.value}`; }},
        replaySources.map(item => el('option', {value:item.id, selected:item.id === detail.id},
          `v${item.version} · ${item.name}`))),
      link('用此版本回放', `#replay-run/${detail.id}`, {class:'quiet'})),
    p(`${detail.filename} · ${detail.ticket_count} 张到票 · ${detail.shift_count} 个班次 · ${date(detail.created_at)}`, 'filename'),
    detail.parent_id ? p(`基于 v${detail.version - 1} 增量导入，最早重算班次下标 ${detail.boundary}，更早班次结果保留。`, 'subtle')
      : p('首个数据源版本，全量回放。', 'subtle'),
    detail.warnings?.length && el('ul', {class:'method-list'}, detail.warnings.map(text => el('li', {}, text))),
    table([
      {key:'ticket_id', label:'票据编号'}, {key:'arrived_at', label:'到票时间'},
      {key:'a_pass', label:'A 通过'}, {key:'b_pass', label:'B 通过'},
    ], detail.tickets, {caption:'到票记录', pageSize:10}),
    table([
      {key:'shift_id', label:'班次'}, {key:'start_at', label:'开始'}, {key:'end_at', label:'结束'},
      {key:'quota_a', label:'A 名额'}, {key:'quota_b', label:'B 名额'},
    ], detail.shifts, {caption:'班次名额', pageSize:10}))
    : empty('还没有回放数据源。请导入到票记录和班次名额两份 CSV。');

  const submit = button('校验并导入新版本', {type:'submit', class:'primary', 'data-testid':'replay-upload'});
  const upload = session.canWrite && el('form', {class:'fields', onSubmit:handler(submit, error, async event => {
    const data = new FormData(event.target);
    const ticketFile = data.get('ticket_csv');
    const shiftFile = data.get('shift_csv');
    if (!ticketFile?.size || !shiftFile?.size) throw new Error('请同时选择到票记录和班次名额两个 CSV 文件');
    if (ticketFile.size > 2_000_000 || shiftFile.size > 2_000_000) throw new Error('单个 CSV 最多 2 MB');
    const item = await workspace.replayUpload({
      name:data.get('name') || '补录回放数据源',
      filename:ticketFile.name,
      ticket_csv:await ticketFile.text(),
      shift_csv:await shiftFile.text(),
      parent_id:replaySources[0]?.id ?? null,
    });
    const target = `#replay-inputs/${item.id}`;
    if (location.hash === target) await refresh();
    else location.hash = target;
  })},
    el('label', {class:'field', for:'replay-name'}, el('span', {}, '数据源名称'),
      el('input', {id:'replay-name', name:'name', type:'text', maxlength:120,
        placeholder:'澄石零售补录回放数据源', required:true})),
    fileField('ticket_csv', '到票记录 CSV（ticket_id, arrived_at, a_pass, b_pass）'),
    fileField('shift_csv', '班次名额 CSV（shift_id, start_at, end_at, quota_a, quota_b）'),
    submit, error);

  const versions = table([
    {label:'版本', render:row => link(`v${row.version}`, `#replay-inputs/${row.id}`)},
    {key:'name', label:'名称'},
    {key:'ticket_count', label:'到票数'},
    {key:'shift_count', label:'班次数'},
    {label:'重算边界', render:row => row.boundary === null ? '全量' : (row.boundary >= row.shift_count ? '无变化' : `第 ${row.boundary + 1} 班起重算`)},
    {label:'时间', render:row => date(row.created_at)},
  ], replaySources, {caption:'数据源版本', empty:'导入后在此保留每个版本和数据指纹。'});

  return el('div', {class:'two-col'},
    el('div', {class:'wide-panel'}, preview, panel('版本历史', versions)),
    el('aside', {class:'sidebar-panel'},
      panel('导入数据源', p('到票时间或判定更新后再次导入，系统按最早受影响班次增量重算，历史班次结果保留。', 'subtle'),
        ticketSample, shiftSample, upload),
      panel('回放口径', el('ul', {class:'method-list'}, replaySpec.rules.map(text => el('li', {}, text))))));
}
