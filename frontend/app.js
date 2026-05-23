// AI Growth Copilot · 前端单页
// 设计要点：
//   - 没有任何构建工具，原生 JS
//   - 每次点"开始分析"重新生成一个 demo external_id（视为一条新客户）；
//     服务端再生成 analysis_id（UUID），feedback 时优先用 analysis_id 精准 join
//   - window.currentExternalId / window.currentAnalysisId 全局存（不写 localStorage）
//   - 反馈卡片：在 analyze 成功且有 analysis_id 时才显示
//   - 顶栏 "📚 方法论" 浮层：CRUD 自定义 playbook
//   - 顶栏 "📊 反馈统计" 浮层：拉 /api/analytics/feedback 渲染混淆矩阵 / 打脸案例

(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  // playbook 文件名校验（前端先拦一道，后端二次校验是权威）
  const PLAYBOOK_NAME_RE = /^[a-zA-Z0-9_\-]{1,40}$/;

  // -------- 初始化 --------

  async function init() {
    window.currentExternalId = null;
    window.currentAnalysisId = null;
    await Promise.all([loadHealth(), loadLeads()]);
    $('#analyze-btn').addEventListener('click', onAnalyze);
    $('#lead-select').addEventListener('change', onSelectLead);
    $('#feedback-submit-btn').addEventListener('click', onSubmitFeedback);

    // 顶栏按钮
    $('#open-playbooks-btn').addEventListener('click', openPlaybookModal);
    $('#open-analytics-btn').addEventListener('click', openAnalyticsModal);
    $$('[data-close-modal]').forEach(el => el.addEventListener('click', (e) => {
      const which = e.currentTarget.dataset.closeModal;
      closeModal(which);
    }));

    // Playbook 编辑面板按钮
    $('#pb-new-btn').addEventListener('click', onNewPlaybook);
    $('#pb-reload-btn').addEventListener('click', loadPlaybookList);
    $('#pb-save-btn').addEventListener('click', onSavePlaybook);
    $('#pb-delete-btn').addEventListener('click', onDeletePlaybook);
  }

  // 生成新的 demo external_id（每次点"开始分析"调用，模拟新客户）
  function freshExternalId() {
    return 'demo-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
  }

  async function loadHealth() {
    const pill = $('#health-pill');
    try {
      const r = await fetch('/api/health');
      const body = await r.json();
      if (body.ok) {
        pill.textContent = `后端就绪 · ${body.skill}`;
        pill.className = 'pill pill-ok';
        $('#skill-version').textContent = body.skill;
        $('#max-turns').textContent = body.react_max_turns;
      } else {
        pill.textContent = '后端异常：' + (body.error || '');
        pill.className = 'pill pill-err';
      }
    } catch (e) {
      pill.textContent = '后端不可达';
      pill.className = 'pill pill-err';
    }
  }

  async function loadLeads() {
    const sel = $('#lead-select');
    try {
      const r = await fetch('/api/leads');
      const body = await r.json();
      sel.innerHTML = '<option value="">— 自定义 / 不选 —</option>';
      body.leads.forEach((lead, idx) => {
        const opt = document.createElement('option');
        opt.value = idx;
        opt.textContent = `${lead.lead_id} · ${lead.label}`;
        opt.dataset.payload = JSON.stringify(lead);
        sel.appendChild(opt);
      });
    } catch (e) {
      sel.innerHTML = '<option value="">— 加载失败 —</option>';
    }
  }

  function onSelectLead(e) {
    const opt = e.target.selectedOptions[0];
    if (!opt || !opt.dataset.payload) return;
    const lead = JSON.parse(opt.dataset.payload);
    const text = formatLeadAsText(lead);
    $('#lead-text').value = text;
  }

  function formatLeadAsText(lead) {
    const lines = [];
    if (lead.source) lines.push(`来源：${lead.source}`);
    if (lead.company) lines.push(`公司：${lead.company}`);
    if (lead.industry) lines.push(`行业：${lead.industry}`);
    if (lead.interested_product) lines.push(`感兴趣产品：${lead.interested_product}`);
    if (lead.customer_message) {
      lines.push('');
      lines.push('客户留言：');
      lines.push(lead.customer_message);
    }
    if (lead.sales_note) {
      lines.push('');
      lines.push(`销售备注：${lead.sales_note}`);
    }
    return lines.join('\n');
  }

  // -------- 分析 --------

  async function onAnalyze() {
    const text = $('#lead-text').value.trim();
    if (!text) {
      alert('请输入或选择一条线索');
      return;
    }
    const btn = $('#analyze-btn');
    const status = $('#analyze-status');
    btn.disabled = true;
    status.textContent = '分析中（最多 ~30 秒）…';
    $('.result-pane').hidden = true;
    $('.trace-pane').hidden = true;
    $('.feedback-pane').hidden = true;

    // 每次点击"开始分析"重新生成 external_id（视为新一条线索 / 模拟新客户）
    // 服务端会再生成 analysis_id 回传，存到 window.currentAnalysisId
    window.currentExternalId = freshExternalId();
    window.currentAnalysisId = null;
    $('#id-tag-external').textContent = window.currentExternalId;
    $('#id-tag-analysis').textContent = '（待返回）';
    $('#id-tag-wrap').hidden = false;
    // 清空反馈表单
    $('#feedback-status').textContent = '';
    $('#feedback-note').value = '';
    $('#feedback-amount').value = '';
    $('#feedback-outcome').value = 'pending';

    const t0 = performance.now();
    try {
      const r = await fetch('/api/analyze', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({lead_text: text, external_id: window.currentExternalId}),
      });
      const body = await r.json();
      const ms = Math.round(performance.now() - t0);
      status.textContent = `完成 · ${ms}ms`;
      if (!body.ok) {
        alert('分析失败：' + body.error);
      }
      // 即便兜底分支，body.analysis_id 也应该有
      if (body.analysis_id) {
        window.currentAnalysisId = body.analysis_id;
        $('#id-tag-analysis').textContent = body.analysis_id.slice(0, 8) + '…';
      }
      if (body.result) {
        renderResult(body.result);
        $('.result-pane').hidden = false;
        // 只要本次分析返回了 analysis_id，反馈卡片就可用
        if (window.currentAnalysisId) {
          $('.feedback-pane').hidden = false;
        }
      }
      if (body.trace) {
        renderTrace(body.trace);
        $('.trace-pane').hidden = false;
      }
    } catch (e) {
      status.textContent = '请求出错';
      alert('请求出错：' + e.message);
    } finally {
      btn.disabled = false;
    }
  }

  // -------- 反馈 --------

  async function onSubmitFeedback() {
    if (!window.currentAnalysisId) {
      alert('当前没有 analysis_id，请先点"开始分析"');
      return;
    }
    const payload = {
      analysis_id: window.currentAnalysisId,
      external_id: window.currentExternalId,    // 同时携带，方便客户维度聚合
      outcome: $('#feedback-outcome').value,
      note: $('#feedback-note').value || null,
    };
    const amt = $('#feedback-amount').value;
    if (amt) payload.deal_amount = Number(amt);

    const btn = $('#feedback-submit-btn');
    btn.disabled = true;
    $('#feedback-status').textContent = '提交中…';
    try {
      const r = await fetch('/api/feedback', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const txt = await r.text();
        $('#feedback-status').textContent = '提交失败: ' + r.status;
        alert('提交失败: ' + txt);
        return;
      }
      $('#feedback-status').textContent = '✅ 已记录';
    } catch (e) {
      $('#feedback-status').textContent = '提交出错';
      alert('提交出错: ' + e.message);
    } finally {
      btn.disabled = false;
    }
  }

  // -------- 渲染结果 --------

  function renderResult(r) {
    const el = $('#result');
    el.innerHTML = '';

    // 顶部 summary
    const summary = document.createElement('div');
    summary.className = 'result-summary';
    summary.innerHTML = `
      <span class="tier-badge tier-${escAttr(r.lead_tier)}">${esc(r.lead_tier || '?')} 级</span>
      <span class="stat">意向 <b>${esc(r.intent_level || '?')}</b></span>
      ${r.recommended_product ? `<span class="stat">推荐 <b>${esc(r.recommended_product)}</b></span>` : ''}
      ${r.needs_human_review ? '<span class="review-flag">⚠️ 需人工复核</span>' : ''}
    `;
    el.appendChild(summary);

    if (r.triggered_rules && r.triggered_rules.length) {
      el.appendChild(kv('触发规则', renderChips(r.triggered_rules, 'chip-warn')));
    }

    if (r.pain_points && r.pain_points.length) {
      el.appendChild(kv('客户痛点', renderList(r.pain_points)));
    }
    if (r.missing_info && r.missing_info.length) {
      el.appendChild(kv('缺失信息', renderList(r.missing_info)));
    }
    if (r.risks && r.risks.length) {
      el.appendChild(kv('风险点', renderList(r.risks)));
    }
    if (r.next_actions && r.next_actions.length) {
      el.appendChild(kv('下一步动作', renderList(r.next_actions)));
    }

    if (r.draft_reply) {
      const v = document.createElement('div');
      v.className = 'draft-reply';
      v.textContent = r.draft_reply;
      el.appendChild(kv('给销售的话术草稿', v));
    }

    if (r.evidence && r.evidence.length) {
      el.appendChild(kv('Evidence（事实声明 → source_id）', renderEvidence(r.evidence)));
    }

    if (r.tool_iteration_requests && r.tool_iteration_requests.length) {
      const det = document.createElement('details');
      det.innerHTML = `<summary><b>🔧 工具迭代请求（${r.tool_iteration_requests.length} 条）</b></summary>` +
                      `<pre>${esc(JSON.stringify(r.tool_iteration_requests, null, 2))}</pre>`;
      el.appendChild(kv('Tool Iteration Requests', det));
    }
  }

  function kv(k, vNode) {
    const wrap = document.createElement('div');
    wrap.className = 'kv';
    const kEl = document.createElement('div');
    kEl.className = 'k';
    kEl.textContent = k;
    const vEl = document.createElement('div');
    vEl.className = 'v';
    if (typeof vNode === 'string') vEl.textContent = vNode;
    else vEl.appendChild(vNode);
    wrap.appendChild(kEl);
    wrap.appendChild(vEl);
    return wrap;
  }

  function renderList(items) {
    const ul = document.createElement('ul');
    items.forEach(it => {
      const li = document.createElement('li');
      li.textContent = typeof it === 'string' ? it : JSON.stringify(it);
      ul.appendChild(li);
    });
    return ul;
  }

  function renderChips(items, extraClass) {
    const wrap = document.createElement('div');
    items.forEach(it => {
      const c = document.createElement('span');
      c.className = 'chip ' + (extraClass || '');
      c.textContent = it;
      wrap.appendChild(c);
    });
    return wrap;
  }

  function renderEvidence(rows) {
    const tbl = document.createElement('table');
    tbl.className = 'evidence-table';
    rows.forEach(({claim, source_id}) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${esc(claim || '')}</td><td>${esc(source_id || '')}</td>`;
      tbl.appendChild(tr);
    });
    return tbl;
  }

  // -------- 渲染 Trace --------

  function renderTrace(steps) {
    const ol = $('#trace-timeline');
    ol.innerHTML = '';
    steps.forEach(step => {
      const li = document.createElement('li');
      li.className = 'trace-step trace-step-' + step.step_type;

      const det = document.createElement('details');
      const sum = document.createElement('summary');
      const latency = step.latency_ms != null
        ? `<span class="latency">${step.latency_ms}ms</span>` : '';
      const summaryText = step.output_summary
        || step.context_summary
        || (step.tool_name ? `${step.tool_name}` : '')
        || (step.error ? `⚠️ ${step.error}` : '');
      sum.innerHTML = `
        <span class="tag">${esc(step.step_type)}</span>
        <span class="summary-text">${esc(summaryText)}</span>
        ${latency}
      `;
      det.appendChild(sum);

      const pre = document.createElement('pre');
      pre.textContent = JSON.stringify(step, null, 2);
      det.appendChild(pre);

      li.appendChild(det);
      ol.appendChild(li);
    });
  }

  // -------- Playbook 编辑 --------

  function openPlaybookModal() {
    $('#playbook-modal').hidden = false;
    loadPlaybookList();
  }

  function closeModal(which) {
    if (which === 'playbook') $('#playbook-modal').hidden = true;
    if (which === 'analytics') $('#analytics-modal').hidden = true;
  }

  async function loadPlaybookList() {
    const ul = $('#pb-list');
    ul.innerHTML = '<li class="pb-loading">加载中…</li>';
    try {
      const r = await fetch('/api/playbooks');
      const body = await r.json();
      ul.innerHTML = '';
      if (!body.items || body.items.length === 0) {
        ul.innerHTML = '<li class="pb-empty">（暂无 playbook）</li>';
        return;
      }
      body.items.forEach(item => {
        const li = document.createElement('li');
        li.className = 'pb-item';
        li.innerHTML = `
          <div class="pb-item-title">${esc(item.title)}</div>
          <div class="pb-item-meta">
            <code>${esc(item.name)}</code> · ${esc(item.updated_at)} · ${item.size}B
          </div>
        `;
        li.addEventListener('click', () => loadPlaybookContent(item.name));
        ul.appendChild(li);
      });
    } catch (e) {
      ul.innerHTML = '<li class="pb-empty">加载失败</li>';
    }
  }

  async function loadPlaybookContent(name) {
    $('#pb-status').textContent = '';
    try {
      const r = await fetch('/api/playbooks/' + encodeURIComponent(name));
      if (!r.ok) {
        $('#pb-status').textContent = '加载失败 ' + r.status;
        return;
      }
      const body = await r.json();
      // name 不含 .md（前端编辑时只让用户填 stem）
      $('#pb-name').value = name.replace(/\.md$/, '');
      $('#pb-title').value = body.title || '';
      $('#pb-body').value = body.body || body.content || '';
    } catch (e) {
      $('#pb-status').textContent = '加载出错: ' + e.message;
    }
  }

  function onNewPlaybook() {
    $('#pb-name').value = '';
    $('#pb-title').value = '';
    $('#pb-body').value = '';
    $('#pb-status').textContent = '新建中…填好名字后点保存';
  }

  async function onSavePlaybook() {
    const stem = ($('#pb-name').value || '').trim();
    if (!PLAYBOOK_NAME_RE.test(stem)) {
      alert('文件名只能含字母 / 数字 / _ / -，长度 1-40');
      return;
    }
    const title = $('#pb-title').value.trim();
    const body = $('#pb-body').value;
    const name = stem + '.md';
    $('#pb-status').textContent = '保存中…';
    try {
      const r = await fetch('/api/playbooks/' + encodeURIComponent(name), {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({content: body, title: title || null}),
      });
      if (!r.ok) {
        const txt = await r.text();
        $('#pb-status').textContent = '保存失败: ' + r.status;
        alert('保存失败：' + txt);
        return;
      }
      $('#pb-status').textContent = '✅ 已保存';
      loadPlaybookList();
    } catch (e) {
      $('#pb-status').textContent = '保存出错';
      alert(e.message);
    }
  }

  async function onDeletePlaybook() {
    const stem = ($('#pb-name').value || '').trim();
    if (!stem) {
      alert('请先选中要删除的 playbook');
      return;
    }
    if (!confirm(`确认删除 ${stem}.md ?`)) return;
    const name = stem + '.md';
    try {
      const r = await fetch('/api/playbooks/' + encodeURIComponent(name), {method: 'DELETE'});
      if (!r.ok) {
        const txt = await r.text();
        alert('删除失败：' + txt);
        return;
      }
      $('#pb-name').value = '';
      $('#pb-title').value = '';
      $('#pb-body').value = '';
      $('#pb-status').textContent = '🗑 已删除';
      loadPlaybookList();
    } catch (e) {
      alert('删除出错: ' + e.message);
    }
  }

  // -------- 反馈统计 --------

  async function openAnalyticsModal() {
    $('#analytics-modal').hidden = false;
    const dest = $('#analytics-body');
    dest.innerHTML = '加载中…';
    try {
      const r = await fetch('/api/analytics/feedback');
      const body = await r.json();
      dest.innerHTML = '';

      // 顶部统计
      const top = document.createElement('div');
      top.className = 'analytics-top';
      const breakdown = body.match_kind_breakdown || {};
      top.innerHTML = `
        <span class="stat-pill">总反馈 <b>${body.total_feedback}</b></span>
        <span class="stat-pill">未匹配 <b>${body.no_match_feedback_count}</b></span>
        <span class="stat-pill">精准/模糊/孤立 <b>${breakdown.precise || 0}/${breakdown.fuzzy || 0}/${breakdown.orphan || 0}</b></span>
        <span class="stat-pill">打脸 <b>${(body.surprises || []).length}</b></span>
      `;
      dest.appendChild(top);

      // 混淆矩阵
      const h = document.createElement('h4');
      h.textContent = '混淆矩阵（预测 lead_tier × outcome）';
      dest.appendChild(h);

      const tbl = document.createElement('table');
      tbl.className = 'cm-table';
      const outcomes = ['deal', 'no_deal', 'pending', 'lost'];
      const tiers = Object.keys(body.confusion_matrix || {});
      // 保证 A/B/C/D 总在前面
      const orderedTiers = ['A', 'B', 'C', 'D', ...tiers.filter(t => !'ABCD'.includes(t))];
      const seen = new Set();
      const finalTiers = orderedTiers.filter(t => {
        if (seen.has(t)) return false;
        seen.add(t);
        return body.confusion_matrix && body.confusion_matrix[t] !== undefined;
      });

      let header = '<tr><th>预测↓ \\ 实际→</th>';
      outcomes.forEach(o => header += `<th>${o}</th>`);
      header += '</tr>';
      tbl.innerHTML = header;
      finalTiers.forEach(tier => {
        const row = body.confusion_matrix[tier] || {};
        let html = `<tr><th>${esc(tier)}</th>`;
        outcomes.forEach(o => {
          const v = row[o] || 0;
          html += `<td class="${v > 0 ? 'has-val' : 'zero-val'}">${v}</td>`;
        });
        html += '</tr>';
        tbl.insertAdjacentHTML('beforeend', html);
      });
      dest.appendChild(tbl);

      // Surprises
      const h2 = document.createElement('h4');
      h2.textContent = '打脸案例（高估 A → no_deal/lost，低估 D → deal）';
      dest.appendChild(h2);
      if (!body.surprises || body.surprises.length === 0) {
        const p = document.createElement('p');
        p.className = 'hint';
        p.textContent = '（暂无打脸案例）';
        dest.appendChild(p);
      } else {
        const ul = document.createElement('ul');
        ul.className = 'surprises';
        body.surprises.forEach(s => {
          const li = document.createElement('li');
          // 优先展示 analysis_id（精准），fallback 到 external_id；同时 badge 一下 match_kind
          const idLabel = s.analysis_id
            ? `<code title="${esc(s.analysis_id)}">${esc(s.analysis_id.slice(0, 8))}…</code>`
            : `<code>${esc(s.external_id || '—')}</code>`;
          const kindBadge = s.match_kind
            ? `<span class="chip chip-${esc(s.match_kind)}">${esc(s.match_kind)}</span>`
            : '';
          li.innerHTML = `
            ${idLabel} ${kindBadge} ·
            预测 <b>${esc(s.predicted_tier)}</b> →
            实际 <b>${esc(s.outcome)}</b>
            <span class="ts">${esc(s.timestamp)}</span>
            ${s.note ? `<div class="note">${esc(s.note)}</div>` : ''}
          `;
          ul.appendChild(li);
        });
        dest.appendChild(ul);
      }
    } catch (e) {
      dest.innerHTML = '加载失败: ' + esc(e.message);
    }
  }

  // -------- helpers --------
  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function escAttr(s) {
    return String(s ?? '').replace(/[^A-Za-z0-9_-]/g, '');
  }

  init();
})();
