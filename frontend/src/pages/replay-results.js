import {feedback} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {REPLAY_TICKET_STATUS, STATUS} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';

const TICKET_STATUS_TEXT = REPLAY_TICKET_STATUS;

function backlogCell(value, worse) {
  return el('span', {class: worse ? 'metric-warn' : ''}, String(value));
}

export async function replayResults(route, refresh) {
  const {replayRuns, replaySpec} = workspace;
  const code = route.id || replayRuns[0]?.code;
  if (!code) return panel('补录验收', empty('还没有回放运行。', '#replay-run', '前往回放'));
  const group = await workspace.replayRun(code);
  const message = feedback();

  if (!group.all_complete) {
    const original = location.hash;
    const status = el('p', {role:'status'}, '两套引擎仍在计算，完成并通过门禁后此页一次性展示结果。');
    poll(() => workspace.replayRun(code), item => item.all_complete, () => location.hash === original)
      .then(item => { if (item && status.isConnected) refresh(); })
      .catch(error => { if (status.isConnected) status.textContent = error.message; });
    return panel(group.name,
      ...['A', 'B'].map(key => el('div', {}, pill(`引擎 ${key} · ${STATUS[group.engines[key].status]}`, group.engines[key].status))),
      status, button('刷新', {class:'quiet', onClick:refresh}));
  }

  // 任一套引擎失败：没有可比结果，展示错误并引导重新发起。
  const failedEngine = ['A', 'B'].find(key => group.engines[key].status === 'failed');
  if (failedEngine || !group.merged) {
    return panel(group.name,
      pill(`引擎 ${failedEngine || ''} 运行失败`, 'failed'),
      p(failedEngine ? (group.engines[failedEngine].error || '计算失败，请重新发起回放。') : '结果尚未生成。', 'feedback'),
      link('重新发起回放', '#replay-run', {class:'quiet'}));
  }

  const engineA = group.engines.A;
  const engineB = group.engines.B;
  const blocked = group.stale_blocked;
  const gateNote = group.published
    ? pill('两套引擎均完成，验收页与导出已一次性发布', 'complete')
    : (blocked ? pill('存在晚到的旧计算结果，发布已拦截，请重新发起回放', 'failed')
               : pill('等待发布门禁', 'queued'));

  const summaryA = engineA.summary;
  const summaryB = engineB.summary;
  const headline = blocked ? '回放结果已拦截，不能作为切换依据。'
    : `候选 B 班末积压较 A 最多低 ${Math.max(...group.merged.map(row => -row.backlog_delta))} 张，超时票 ${summaryB.overdue_total} 张（A 为 ${summaryA.overdue_total} 张）。`;

  const mergedTable = table([
    {key:'shift_id', label:'班次'},
    {key:'start_at', label:'开始'},
    {key:'end_at', label:'结束'},
    {label:'A 班末积压', render:row => backlogCell(row.backlog_a, row.backlog_a > 0)},
    {label:'B 班末积压', render:row => backlogCell(row.backlog_b, row.backlog_b > 0)},
    {label:'积压差 B−A', render:row => el('span', {class:row.backlog_delta <= 0 ? 'metric-good' : 'metric-warn'},
      `${row.backlog_delta > 0 ? '+' : ''}${row.backlog_delta}`)},
    {label:'A 累计超时', render:row => backlogCell(row.overdue_a, row.overdue_a > 0)},
    {label:'B 累计超时', render:row => backlogCell(row.overdue_b, row.overdue_b > 0)},
    {label:'结果来源', render:row => row.retained ? pill('保留旧结果', 'queued') : pill('重算结果', 'complete')},
  ], group.merged, {caption:'各班积压与超时', empty:'暂无班次结果。'});

  const metricCards = metrics([
    {label:'A 超时票（累计）', value:summaryA.overdue_total, detail:`仍排队 ${summaryA.pending_total} · 已补录 ${summaryA.processed_total}`},
    {label:'B 超时票（累计）', value:summaryB.overdue_total, detail:`仍排队 ${summaryB.pending_total} · 已补录 ${summaryB.processed_total}`},
    {label:'A 失败票据', value:summaryA.failed_total},
    {label:'B 失败票据', value:summaryB.failed_total, detail:`较 A 少 ${summaryA.failed_total - summaryB.failed_total} 张`},
  ]);

  // 超时票查询：默认展示 B 的全部超时票，可切换引擎与状态。
  const state = {engine:'B', status:'overdue', rows:[], total:0};
  const ticketBody = el('tbody');
  const ticketTable = el('table', {},
    el('caption', {class:'sr'}, '票据补录轨迹'),
    el('thead', {}, el('tr', {}, ['票据编号', '到票时间', '到票班次', '状态', '补录班次'].map(label => el('th', {scope:'col'}, label)))),
    ticketBody);
  const ticketMeta = el('p', {class:'subtle'});
  async function loadTickets() {
    const data = await workspace.replayTickets(code, state.engine, state.status);
    state.rows = data.rows;
    state.total = data.total;
    ticketBody.replaceChildren(...data.rows.map(row => el('tr', {},
      el('td', {}, row.ticket_id), el('td', {}, row.arrived_at), el('td', {}, row.arrival_shift),
      el('td', {}, TICKET_STATUS_TEXT[row.status] || row.status),
      el('td', {}, row.processed_shift || '—'))));
    ticketMeta.textContent = `引擎 ${state.engine} · 共 ${data.total} 张（每页最多 50 张）`;
  }
  const engineSelect = el('select', {onChange:async event => { state.engine = event.target.value; await loadTickets(); }},
    ['A', 'B'].map(value => el('option', {value, selected:value === state.engine}, `引擎 ${value}`)));
  const statusSelect = el('select', {onChange:async event => { state.status = event.target.value; await loadTickets(); }},
    [['all', '全部票据'], ['overdue', '仅超时票'], ['pending_overdue', '超时未补'],
     ['done_overdue', '超时后补录'], ['pending', '排队中'], ['done', '已补录'], ['unreached', '晚于末班']]
      .map(([value, label]) => el('option', {value, selected:value === state.status}, label)));
  await loadTickets();

  const compareButton = button('下载双引擎对比 CSV', {class:'quiet', 'data-testid':'replay-compare-export'});
  compareButton.addEventListener('click', handler(compareButton, message, () =>
    workspace.download(`/replay/runs/${code}/comparison`, `replay-comparison-${code}.csv`)));
  const engineButtons = ['A', 'B'].map(key => {
    const btn = button(`下载引擎 ${key} 明细 CSV`, {class:'quiet'});
    btn.addEventListener('click', handler(btn, message, () =>
      workspace.download(`/replay/runs/engine/${group.engines[key].id}/export`, `replay-engine-${key}-${code}.csv`)));
    return btn;
  });
  const exportButtons = group.published ? el('div', {class:'actions'}, compareButton, ...engineButtons)
    : p('导出在门禁通过后开放。', 'subtle');

  return el('div', {},
    el('section', {class:'analysis'},
      el('div', {class:'result-head'}, p(group.name, 'eyeline'), h(2, headline), gateNote, message)),
    el('div', {class:'two-col'},
      el('div', {class:'wide-panel'},
        panel('各班积压与超时', mergedTable),
        panel('票据补录轨迹', el('div', {class:'actions'}, engineSelect, statusSelect), ticketMeta,
          el('div', {class:'table-wrap'}, ticketTable))),
      el('aside', {class:'sidebar-panel'},
        panel('双引擎汇总', metricCards),
        panel('发布与导出', exportButtons,
          link('查看回放运行', `#replay-run`, {class:'quiet'})),
        panel('口径说明', el('ul', {class:'method-list'}, replaySpec.rules.map(text => el('li', {}, text)))))));
}
