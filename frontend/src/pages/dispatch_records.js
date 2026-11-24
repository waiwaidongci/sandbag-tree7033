import {api} from '../api/client.js';
import {empty,errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {time} from '../utils/html.js';
import {chooseRun,currentRun} from './analysis.js';

const dispatchStates = {draft:'草稿', accepted:'已受理', finished:'已完成', superseded:'已被新版本替代'};

function payloadField(value) {
  // 每批一行：批次号,装车位,排车时间,实耗分钟
  return value.trim().split(/\r?\n/).map(line => {
    const [batch_id, loading_bay, planned_at, elapsed_min] = line.split(',').map(cell => cell.trim());
    return {batch_id, loading_bay, planned_at, elapsed_min: Number(elapsed_min)};
  });
}

export async function dispatchRecords() {
  const [{runs,run}, box] = await Promise.all([currentRun(), api('/dispatch')]);
  const operator = state.account.role === 'analyst' || state.account.role === 'reviewer';
  const createForm = operator && run?.result?.selected_rules ? `<form id="dispatch-create" class="inline-form" data-id="${run.id}">
    <label>排车单号<input name="dispatch_no" minlength="2" maxlength="60" required placeholder="如 PC-20261124-01"></label>
    <label>批次明细（每行：批次号,装车位,排车时间,实耗分钟）<textarea name="payload_text" rows="5" required placeholder="SD-24-01,东闸一号位,2026-11-24 07:00,47"></textarea></label>
    <button type="submit">建立排车记录（草稿）</button>${errorSlot}</form>` : empty('选择一条含定型规则的已计算任务后，可建立排车记录。');
  const recordTables = box.length ? table(box.map(row => ({
    记录: row.id, 排车单号: row.dispatch_no, 版本: row.version, 任务: row.run_id,
    状态: dispatchStates[row.state] ?? row.state,
    名额: row.options_snapshot.alert_quota,
    受理重放: row.replay ? `代价 ${row.replay.totals.cost} · 发出 ${row.replay.totals.sent} · 满额跳过 ${row.replay.totals.skipped}` : '—',
    建立时间: time(row.created), 受理时间: time(row.accepted),
  }))) : '<p class="empty">还没有排车记录。</p>';
  return `<section>${run ? chooseRun(runs, run.id) : ''}<h2>建立排车记录</h2>
    <p class="hint">受理时按该任务定型的两处装车位规则冻结逐批结论，并用任务参数中的每小时名额回放；并发更正会另存新版本，不覆盖原记录。</p>
    ${createForm}</section>
    <section><h2>排车记录版本</h2>${recordTables}
    <form id="dispatch-action" class="inline-form">
      <label>记录编号<input name="record_id" type="number" min="1" required></label>
      <button type="submit" value="accept" ${operator ? '' : 'disabled'}>受理并冻结重放</button>
      <button type="submit" value="resume">中断恢复核对</button>
      <button type="submit" value="finish" ${operator ? '' : 'disabled'}>完装签收</button>
      ${errorSlot}
    </form>
    ${operator ? `<form id="dispatch-correct" class="inline-form">
      <h3>并发更正（另存新版本，不覆盖原记录）</h3>
      <label>原记录编号<input name="record_id" type="number" min="1" required></label>
      <label>更正后批次明细（每行：批次号,装车位,排车时间,实耗分钟）<textarea name="payload_text" rows="5" required></textarea></label>
      <button type="submit">另存更正版本</button>${errorSlot}
    </form>` : ''}
    <p id="dispatch-result" class="notice" hidden></p>
    <div id="resume-detail"></div></section>`;
}

function resumeTable(detail) {
  const rows = detail.details.map(item => ({
    批次: `${item.batch_id} · ${item.bay}`, 排车时间: item.planned_at, 判断路径: item.trace,
    结论: item.status, 窗口内预警: item.window_alerts,
    实耗: `${item.elapsed_min} 分钟（${item.overdue ? '超时' : '按时'}）`,
    误报: item.false_alert, 漏报: item.miss, 代价: item.cost,
  }));
  const skipped = detail.details.filter(item => item.status === '满额转常规');
  return `<h3>恢复重放（版本 v${detail.version} · ${dispatchStates[detail.state]}）</h3>
    <p class="hint">受理冻结代价 ${JSON.stringify(detail.frozen_totals)} ｜ 恢复重放代价 ${JSON.stringify(detail.replay_totals)} ｜ 一致性：
    <strong>${detail.consistent ? '一致（仍按受理时版本与参数）' : '不一致，请复核'}</strong></p>
    <p class="hint">因满额跳过 ${skipped.length} 批。</p>${table(rows)}`;
}

export function bindDispatchRecords(root, refresh) {
  bindForm(root, '#dispatch-create', async form => {
    const runId = Number(form.dataset.id);
    const payload = payloadField(form.elements.payload_text.value);
    if (!payload.length || payload.some(row => !row.batch_id || !row.loading_bay || !row.planned_at || !Number.isFinite(row.elapsed_min))) {
      throw new Error('每行需填写 批次号,装车位,排车时间,实耗分钟。');
    }
    await api('/dispatch', {dispatch_no: form.elements.dispatch_no.value, run_id: runId, payload});
    state.status = '排车记录草稿已建立。';
  }, refresh);
  bindForm(root, '#dispatch-action', async (form, button) => {
    const key = form.elements.record_id.value;
    const slot = root.querySelector('#dispatch-result');
    const detailBox = root.querySelector('#resume-detail');
    if (button.value === 'resume') {
      const detail = await api(`/dispatch/${key}/resume`);
      detailBox.innerHTML = resumeTable(detail);
      slot.hidden = true;
      return;
    }
    const result = await api(`/dispatch/${key}/${button.value === 'accept' ? 'accept' : 'finish'}`, {});
    slot.hidden = false;
    slot.textContent = button.value === 'accept'
      ? `已受理 #${key}，重放代价 ${result.totals.cost}，发出 ${result.totals.sent} 条，满额跳过 ${result.totals.skipped} 批。`
      : `记录 #${key} 已完装签收，状态：${dispatchStates[result.state] ?? result.state}。`;
    await refresh();
  });
  bindForm(root, '#dispatch-correct', async form => {
    const key = form.elements.record_id.value;
    const payload = payloadField(form.elements.payload_text.value);
    if (!payload.length || payload.some(row => !row.batch_id || !row.loading_bay || !row.planned_at || !Number.isFinite(row.elapsed_min))) {
      throw new Error('每行需填写 批次号,装车位,排车时间,实耗分钟。');
    }
    const result = await api(`/dispatch/${key}/corrections`, {payload});
    state.status = `已另存排车单 ${result.dispatch_no} 的新版本 v${result.version}（草稿），原记录保留。`;
  }, refresh);
}
