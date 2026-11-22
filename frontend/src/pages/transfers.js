import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {transferStatusLabels} from '../constants/workflow.js';
import {topic} from '../constants/project.js';
import {date, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';
import {go} from '../router/hash.js';

function caseTitle(state, caseId) {
  const found = state.cases.find(row => row.id === Number(caseId));
  return found ? `${found.id} · ${found.title}` : `批次 ${caseId}`;
}

export function transfersPage(state, identity) {
  const isOperator = state.session.role === 'operator';
  const selected = state.transfers.find(row => row.id === identity)
    || state.transfers.find(row => row.status !== 'effective')
    || state.transfers[0];
  const spec = state.transferSpec;
  const caseOptions = state.cases.map(row =>
    `<option value="${row.id}">${esc(row.id)} · ${esc(row.title)}</option>`).join('');
  const sample = spec?.sample || {
    handover_no: '', from_case_id: state.cases[0]?.id || '',
    to_case_id: state.cases[1]?.id || '', from_item_ids: [], to_item_ids: []
  };

  const form = isOperator ? `
  <form id="transfer-form" class="panel">
    <h2>办理转件 / 两批互换</h2>
    <p class="note">${esc(spec?.guidance || '')}</p>
    <div class="field-grid">
      <label class="field"><span>库房交接号</span>
        <input name="handover_no" required maxlength="64" value="${esc(sample.handover_no)}" placeholder="如 HJ-20261122-01"></label>
      <label class="field"><span>转出批次</span>
        <select name="from_case_id" required>${caseOptions}</select></label>
      <label class="field"><span>接收批次</span>
        <select name="to_case_id" required>${caseOptions}</select></label>
      <label class="field data"><span>转出批次移出件号（JSON 数组，互换时两侧都填）</span>
        <textarea name="from_item_ids" rows="2" spellcheck="false">${esc(JSON.stringify(sample.from_item_ids))}</textarea></label>
      <label class="field data"><span>接收批次移出件号（JSON 数组，单向转件可留空数组）</span>
        <textarea name="to_item_ids" rows="2" spellcheck="false">${esc(JSON.stringify(sample.to_item_ids))}</textarea></label>
    </div>
    <div class="actions form-actions"><button>提交转件交接</button>
      <span class="note">两份新顺序单都排定后一起生效；任一份失败仍按原单上架。</span></div>
  </form>` : '';

  const rows = state.transfers.map(row => [
    `<a href="#transfers/${row.id}">${esc(row.handover_no)}</a>`,
    `<span class="status" data-state="${esc(row.status)}">${esc(transferStatusLabels[row.status] || row.status)}</span>`,
    esc(caseTitle(state, row.from_case_id)),
    esc(caseTitle(state, row.to_case_id)),
    esc(row.owner),
    esc(date(row.created_at))
  ]);

  const detail = selected ? renderDetail(state, selected) : '<p class="empty">尚未办理转件交接。</p>';
  return `${heading(topic.modules[4], spec?.summary || '馆藏件登错批次时办理转件，也可两批互换。', '')}
    ${form}
    <div class="spread">
      <section>${records(['交接号', '状态', '转出批次', '接收批次', '经办', '提交时间'], rows, '暂无转件交接。')}</section>
      <aside class="brief">${detail}</aside>
    </div>`;
}

function renderDetail(state, row) {
  const request = row.request_json || {};
  const prepared = row.prepared_json;
  const pair = (label, ids) => ids?.length ? `<p><strong>${esc(label)}</strong>：${esc(ids.join('、'))}</p>` : '';
  const preparedPanels = prepared ? ['from', 'to'].map(side => {
    const part = prepared[side];
    const orderRows = part.report.sections[0].rows.map(item =>
      [item['上架顺位'], item['馆藏件号'], item['馆藏登录号'], item['原登记顺位']]);
    return panel(`新顺序单 · ${esc(caseTitle(state, part.case_id))}（${part.report.metrics['上架件数']} 件）`,
      records(['上架顺位', '馆藏件号', '登录号', '原登记顺位'], orderRows.map(r => r.map(esc))));
  }).join('') : '';
  return `<h2>${esc(row.handover_no)}</h2>
    <p><span class="status" data-state="${esc(row.status)}">${esc(transferStatusLabels[row.status] || row.status)}</span></p>
    ${pair('转出批次移出件', request.from_item_ids)}
    ${pair('接收批次移出件', request.to_item_ids)}
    ${row.error ? `<p class="error">${esc(row.error)}</p>` : ''}
    ${row.status === 'effective' ? '<p class="note">两份新顺序单已在同一事务生效，历史已签顺序单仍可在「上架顺序单」中原样查阅。</p>' : ''}
    ${['queued', 'running', 'prepared'].includes(row.status) ? '<p class="note">后台处理中，列表会自动刷新。</p>' : ''}
    ${preparedPanels}`;
}

export function bindTransfers(state, api, fail, refresh) {
  submit('transfer-form', async data => {
    const payload = {
      handover_no: data.get('handover_no').trim(),
      from_case_id: Number(data.get('from_case_id')),
      to_case_id: Number(data.get('to_case_id')),
      from_item_ids: parseIds(data.get('from_item_ids'), '转出批次移出件号'),
      to_item_ids: parseIds(data.get('to_item_ids'), '接收批次移出件号')
    };
    const result = await api('/transfers', {method: 'POST', data: {payload}});
    go(`transfers/${result.id}`);
    await refresh();
  }, fail);
}

function parseIds(raw, label) {
  try {
    const value = JSON.parse(raw || '[]');
    if (!Array.isArray(value) || value.some(x => typeof x !== 'string' || !x.trim())) {
      throw new Error();
    }
    return value;
  } catch {
    throw new Error(`「${label}」必须是 JSON 字符串数组，如 ["BX-01"]。`);
  }
}
