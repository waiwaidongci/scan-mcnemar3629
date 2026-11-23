export const ROLE = {admin:'复核主管', analyst:'分析员', viewer:'只读查看'};
export const STATUS = {queued:'排队中', running:'正在计算', complete:'计算完成', failed:'运行失败'};
export const REVIEW = {pending:'待复核', accepted:'已通过', returned:'已退回'};
export const REPLAY_TICKET_STATUS = {
  done:'已补录', done_overdue:'已补录（曾超时）', pending:'排队中',
  pending_overdue:'超时未补', unreached:'晚于末班未入队',
};
