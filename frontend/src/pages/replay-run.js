import {feedback} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {STATUS} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

function enginePill(key, view) {
  return el('span', {class:'replay-engine-state'}, `引擎 ${key} `, pill(STATUS[view.status], view.status));
}

function groupRow(group) {
  return el('tr', {},
    el('td', {}, link(group.name, `#replay-results/${group.code}`)),
    el('td', {}, ...['A', 'B'].map(key => el('div', {}, enginePill(key, group.engines[key])))),
    el('td', {}, group.published ? pill('已发布', 'complete') : pill('待门禁', 'queued')),
    el('td', {}, group.stale_blocked ? pill('旧计算已拦截', 'failed') : '—'),
    el('td', {}, date(group.created_at)));
}

export async function replayRun(route, refresh) {
  const {replaySources, replayRuns, replaySpec} = workspace;
  const error = feedback();
  const submit = button('提交双引擎回放', {type:'submit', class:'primary', 'data-testid':'replay-start'});
  const sourceOptions = replaySources.map(item =>
    el('option', {value:item.id, selected:String(item.id) === String(route.id)},
      `v${item.version} · ${item.name} · ${item.ticket_count} 票 / ${item.shift_count} 班`));
  const form = session.canWrite && (replaySources.length ? el('form', {class:'fields',
    onSubmit:handler(submit, error, async event => {
      const data = new FormData(event.target);
      const item = await workspace.replayStart({source_id:Number(data.get('source_id')), name:data.get('name')});
      location.hash = `#replay-results/${item.code}`;
    })},
    el('label', {class:'field', for:'replay-source'}, el('span', {}, '数据源版本'),
      el('select', {id:'replay-source', name:'source_id', required:true}, sourceOptions)),
    el('label', {class:'field', for:'replay-run-name'}, el('span', {}, '运行名称'),
      el('input', {id:'replay-run-name', name:'name', type:'text', maxlength:120,
        value:'补录回放（A/B 并行）', required:true})),
    el('div', {class:'actions'}, submit), error)
    : empty('请先导入补录数据源。', '#replay-inputs', '前往导入'));

  const runTable = replayRuns.length ? el('div', {class:'table-wrap'},
    el('table', {}, el('caption', {class:'sr'}, '回放运行记录'),
      el('thead', {}, el('tr', {}, ['运行', '两套引擎状态', '门禁', '旧计算', '创建时间'].map(label => el('th', {scope:'col'}, label)))),
      el('tbody', {}, replayRuns.map(groupRow))))
    : p('提交后这里会分别显示引擎 A、B 的排队与计算状态。', 'empty');

  // 最新一组仍在跑时自动刷新。
  const latest = replayRuns[0];
  if (latest && !latest.all_complete) {
    poll(() => workspace.replayRun(latest.code), item => item.all_complete, () => location.hash.includes(latest.code))
      .then(item => { if (item) refresh(); }).catch(() => {});
  }

  return el('div', {class:'two-col'},
    el('div', {class:'wide-panel'},
      panel('补录回放', session.canWrite ? form : p('只读角色可以查看历史回放运行。', 'empty')),
      panel('运行记录', runTable)),
    el('aside', {class:'sidebar-panel'},
      panel('发布门禁', p('同一组回放的 A、B 两套引擎都计算完成后，验收页面与导出才一次性切换可用；任一套未完成或被标记为晚到旧计算，都会拦截发布。', 'subtle')),
      panel('处理口径', el('ul', {class:'method-list'}, replaySpec.rules.map(text => el('li', {}, text))))));
}
