export const pages = ['overview', 'inputs', 'run', 'results', 'review', 'delivery',
  'replay-inputs', 'replay-run', 'replay-results'];

// 导航显示名与路由一一对应，补录回放三页是相对现有验收流新增的模块。
export const pageLabels = {
  'replay-inputs': '补录数据源',
  'replay-run': '补录回放',
  'replay-results': '补录验收',
};

export function route() {
  const [page, id, offset] = location.hash.slice(1).split('/');
  return {page:pages.includes(page) ? page : 'overview', id:Number(id) || null, offset:Math.max(0, Number(offset) || 0)};
}
