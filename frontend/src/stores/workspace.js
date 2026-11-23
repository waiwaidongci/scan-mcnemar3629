import {request, saveFile} from '../api/http.js';
import {session} from './session.js';

const call = (path, body) => request(path, {token:session.token, body});
export const workspace = {
  spec: null,
  datasets: [],
  runs: [],
  replaySources: [],
  replayRuns: [],
  async init() {
    this.spec = await request('/spec');
    this.replaySpec = await call('/replay/spec');
  },
  async refresh() {
    [this.datasets, this.runs, this.replaySources, this.replayRuns] = await Promise.all([
      call('/datasets'), call('/runs'), call('/replay/sources'), call('/replay/runs')]);
  },
  getDataset: (id, offset = 0) => call(`/datasets/${id}?offset=${offset}`),
  getRun: id => call(`/runs/${id}`),
  upload: data => call('/datasets', data),
  start: data => call('/runs', data),
  review: (id, data) => call(`/runs/${id}/review`, data),
  audit: () => call('/audit'),
  schedules: () => call('/schedules'),
  deliveries: () => call('/deliveries'),
  schedule: data => call('/schedules', data),
  disable: id => call(`/schedules/${id}/disable`, {}),
  download: (path, filename) => saveFile(path, session.token, filename),
  replaySource: id => call(`/replay/sources/${id}`),
  replayUpload: data => call('/replay/sources', data),
  replayStart: data => call('/replay/runs', data),
  replayRun: code => call(`/replay/runs/${code}`),
  replayTickets: (code, engine, status) =>
    call(`/replay/runs/${code}/tickets?engine=${engine}&status=${status}`),
};
