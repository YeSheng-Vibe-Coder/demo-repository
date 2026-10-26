/* Alert tracing and rule evolution views (bilingual through L(zh, en) from i18n.js).
 * Data: DATA.trace (validation cases with replay), DATA.showcase, DATA.evaluation. Helpers come from app.js.
 */
window.AMLViews = (() => {
  "use strict";
  let H, DATA, current = null, player = null, view = "trace";
  const ROLE = {
    risk_assessor: ["风险评估", "Risk assessor"], flow_tracer: ["资金流向", "Flow tracer"], structure_checker: ["路径结构", "Structure checker"],
    upstream_tracer: ["上游追踪", "Upstream tracer"], downstream_tracer: ["下游追踪", "Downstream tracer"], baseline_tester: ["基线对比", "Baseline tester"],
    counterparty_profiler: ["对手方画像", "Counterparty profiler"], follow_up: ["追问", "Follow-up"], investigator: ["调查员", "Investigator"],
  };
  const TOOLS = {
    query_between_accounts: ["账户间往来查询", "Between-account transfers", "两个账户之间有没有直接或反向转账？", "Are there direct or reverse transfers between two accounts?", "账户 A、账户 B、时间窗", "account A, account B, window", "双向转账明细（有界）", "Bounded direct and reverse transfers"],
    query_account_transactions: ["账户交易查询", "Account transactions", "窗口内谁付给了这个账户、它又付给了谁？", "Within the window, who paid this account and whom did it pay?", "账户、时间窗、方向", "account, window, direction", "最多 200 笔交易，可翻页", "Up to 200 transactions, paged"],
    check_structure: ["路径结构检测", "Route structure", "资金从哪来、到哪去？路径是汇聚、分散还是回流？", "Where did the funds come from or go; do routes converge, disperse or return?", "账户、方向、最大间隔 6/24/48h、深度 2–5", "account, direction, max gap 6/24/48h, depth 2–5", "Zetaris 多跳追踪：同币种、严格时序、金额连续", "Zetaris multi-hop tracing: same currency, ordered, amount continuity"],
    assess_risk: ["风险评估", "Risk assessment", "这些账户在案发当天风险多高、由哪些特征驱动？", "How risky are these accounts on the case day, and which features drive it?", "1–5 个账户", "1–5 accounts", "每个账户的风险等级、分数和主要驱动特征", "Per account: risk level, score and main drivers"],
    summarize_account: ["账户画像", "Account summary", "这些账户的金额规模、频率和对手方分布？", "What are the payment scale, frequency and counterparties of these accounts?", "1–5 个账户、时间窗", "1–5 accounts, window", "方向 × 币种的笔数、金额、对手方数和首末时间", "Counts, amounts, counterparties and first/last time by direction × currency"],
    compare_account_baseline: ["历史基线对比", "Baseline comparison", "今天和自身历史相比有什么不同？", "How does the case day differ from the account's own history?", "账户、历史 7/14/30 天", "account, history 7/14/30 days", "均值、标准差、z 分数，及今天新对手方占比", "Mean, std, z-score and today's share of new counterparties"],
    get_alert_context: ["告警上下文", "Alert context", "为什么告警、由哪个冻结版本的规则触发？", "Why was this alert raised, and by which frozen rule version?", "无（主机预取）", "none (prefetched by the host)", "触发条件背后的交易与对手方", "Transactions and counterparties behind each triggered condition"],
  };
  const MEMBER = ["#2563EB", "#7C3AED", "#0E9F8E", "#C27C0E"];
  const TIER = { high: ["高风险", "High"], mid: ["中风险", "Medium"], low: ["低风险", "Low"] };
  const DECISION = { report: ["上报", "Report"], review: ["复核", "Review"], dismiss: ["排除", "Dismiss"] };
  const VERDICT = { suspicious: ["可疑", "Suspicious"], not_supported: ["不支持", "Not supported"], insufficient_evidence: ["证据不足", "Insufficient"] };
  const FLAG = {
    onward_within_24h: ["24h 内转出", "Onward within 24h"], to_new_counterparty: ["转给新对手方", "To new counterparty"], currency_switch_on_chain: ["链上换币", "Currency switch"],
    convergence_from_sources: ["多源汇聚", "Convergence"], round_trip: ["资金回流", "Round trip"], counterparty_alerted: ["对手方也被告警", "Counterparty alerted"],
    dormant_then_active: ["沉寂后激活", "Dormant then active"], long_standing_counterparties: ["长期对手方", "Long-standing counterparties"],
    consistent_with_history: ["与历史一致", "Consistent with history"], no_onward_movement: ["无后续转出", "No onward movement"],
  };
  // Per-account outcome of the investigation (see alert_trace.account_verdicts).
  const ACCOUNT = {
    suspicious: ["可疑", "Suspicious", "查过，认定有风险", "examined; judged risky"],
    cleared: ["已排除", "Cleared", "查过，认为无风险", "examined; judged not risky"],
    insufficient: ["证据不足", "Insufficient evidence", "查过，但证据不够下结论", "examined; not enough evidence to decide"],
    not_examined: ["未调查", "Not examined", "出现在交易里，但没有专门查它", "seen in the transactions, not queried on its own"],
    unresolved: ["未定", "Undecided", "", ""], flagged: ["调查员标记", "Flagged", "", ""], observed: ["仅出现", "Seen only", "", ""],
  };
  const POSITION = { focus: ["告警账户", "Alerted"], upstream: ["上游", "Upstream"], downstream: ["下游", "Downstream"], peer: ["关联", "Linked"] };
  const ORDINARY = new Set(["long_standing_counterparties", "consistent_with_history", "no_onward_movement"]);
  const tr = pair => pair ? L(pair[0], pair[1]) : "";
  const ROLE_KEYS = [
    [/follow.?up/, "追问", "Follow-up"], [/benign|legit|innocent|explanation/, "正常解释检验", null], [/upstream|inbound/, "上游追踪", null], [/downstream|outbound/, "下游追踪", null],
    [/baseline/, "基线对比", null], [/counterparty/, "对手方分析", null], [/structure|round.?trip/, "路径结构", null],
    [/risk/, "风险评估", null], [/flow|traffic|route|routing|mapper|transaction/, "资金流向", null], [/lead|generalist/, "主调查员", null],
  ];
  const titleCase = r => String(r).replace(/[_-]+/g, " ").replace(/\s+\d+$/, "").replace(/\b\w/g, ch => ch.toUpperCase());
  const role = r => {
    if (ROLE[r]) return tr(ROLE[r]);
    const key = String(r).toLowerCase(), hit = ROLE_KEYS.find(([re]) => re.test(key));
    return L(hit ? hit[1] : titleCase(r), hit?.[2] || titleCase(r));
  };
  const tool = t => tr(TOOLS[t]?.slice(0, 2)) || t;
  const fname = f => L(DATA.features[f]?.zh || f, f);
  const short = ref => { const [b, a] = String(ref).split(":"); return `${b}:…${(a || "").slice(-4)}`; };
  const pctx = v => v == null ? "—" : (v * 100).toFixed(v >= 0.995 && v < 1 ? 2 : 1) + "%";
  const opx = op => op === "ge" ? "≥" : "≤";

  /* ---------- tier-rule wording ---------- */
  const TAG = {
    amount_vs_history_24h: { ge: ["金额暴增", "amount spike"], le: ["金额平稳", "usual amount"] }, currency_count_24h: { ge: ["多币种", "multi-currency"] },
    history_active_days_7d: { le: ["账户沉寂", "dormant"], ge: ["长期活跃", "active"] }, new_counterparty_share_24h: { ge: ["全新对手方", "new counterparties"], le: ["老对手方", "known counterparties"] },
    onward_currency_switch_48h: { ge: ["换币转出", "currency switch"], le: ["无换币", "no switch"] }, round_trip_48h: { ge: ["资金回流", "round trip"] },
    fan_in_48h: { ge: ["多方汇入", "fan-in"], le: ["汇入少", "low fan-in"] }, fan_in_24h: { ge: ["多方汇入", "fan-in"] }, pass_through_ratio_24h: { ge: ["快速过境", "pass-through"], le: ["过境少", "low pass-through"] },
    transit_gap_hours: { le: ["快速转出", "fast onward"], ge: ["延迟转出", "delayed onward"] }, activity_change_24h: { ge: ["活跃突增", "activity jump"] },
  };
  const tierTitle = t => {
    if (!t?.conditions) return t?.name || "";
    const head = (t.name || "").split("_")[0];
    return [head, ...t.conditions.map(c => tr(TAG[c.feature]?.[c.op]) || fname(c.feature))].join(" · ");
  };
  function why(c) {
    const v = c.actual, f = c.feature, n = H.fmt(v);
    const S = {
      amount_vs_history_24h: [`当天金额是自身历史最大值的 ${n} 倍`, `Today's amount is ${n}× the account's own historical maximum`],
      currency_count_24h: [`当天出现 ${n} 种币种`, `${n} currencies on the day`],
      history_active_days_7d: v === 0 ? ["过去 7 天没有任何交易，突然激活", "No activity in the past 7 days, then suddenly active"] : [`过去 7 天有 ${n} 天有交易`, `Active on ${n} of the past 7 days`],
      new_counterparty_share_24h: [`当天对手方中 ${pctx(v)} 是第一次出现`, `${pctx(v)} of today's counterparties are new`],
      onward_currency_switch_48h: [`48 小时内换币转出 ${n} 次`, `${n} onward currency switches in 48h`],
      round_trip_48h: [`48 小时内回流 ${n} 笔`, `${n} round trips in 48h`],
      fan_in_48h: [`48 小时内 ${n} 个对手方汇入`, `${n} counterparties paid in within 48h`], fan_in_24h: [`24 小时内 ${n} 个对手方汇入`, `${n} counterparties paid in within 24h`],
      pass_through_ratio_24h: [`过境比例 ${n}`, `Pass-through ratio ${n}`], transit_gap_hours: [`收到后 ${n} 小时转出`, `Paid onward ${n}h after receipt`],
      activity_change_24h: [`活跃度为平时的 ${n} 倍`, `Activity ${n}× usual`],
    }[f];
    return S ? tr(S) : `${fname(f)} = ${n}`;
  }
  function segmentStats(name) {
    const ta = DATA.showcase?.tier_agent || {}, chosen = (ta.rounds || []).find(r => r.chosen) || {};
    const parse = line => { const m = /: ([\d,]+) development alerts, ([\d.]+)% laundering/.exec(line || ""); return m ? { alerts: m[1], rate: +m[2] } : null; };
    const own = parse((chosen.segment_results || []).find(l => l.startsWith(name + " ")));
    const unmatched = parse((chosen.segment_results || []).find(l => l.startsWith("unmatched")));
    const seg = (ta.segments || []).find(s => s.name === name);
    const key = s => s.conditions.map(c => c.feature + c.op).sort().join();
    const pattern = seg ? (ta.patterns || []).find(p => key(p) === key(seg)) : null;
    return { own, unmatched, rank: (ta.segments || []).findIndex(s => s.name === name) + 1, total: (ta.segments || []).length, pattern, round: chosen.round, label: ta.label };
  }

  /* ---------- animation helpers ---------- */
  function countUp(node, target, format, ms = 900) {
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) { node.textContent = format(target); return; }
    const t0 = performance.now();
    const tick = now => { const k = Math.min(1, (now - t0) / ms), e = 1 - Math.pow(1 - k, 3); node.textContent = format(target * e); if (k < 1) requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  }
  function typewrite(node, text, onTick) {
    clearInterval(node._tw); node.textContent = ""; let i = 0;
    node._tw = setInterval(() => { i = Math.min(text.length, i + 3); node.textContent = text.slice(0, i); onTick?.(); if (i >= text.length) clearInterval(node._tw); }, 33);
  }
  const grow = root => requestAnimationFrame(() => requestAnimationFrame(() => H.$$("[data-w]", root).forEach(n => n.style.width = n.dataset.w)));

  /* =================== alert tracing =================== */
  // The list pages through every validation alert on the server (/dashboard/api/alerts); investigated alerts carry
  // their recorded case, the others are graded here from their features and the rules / policy in DATA.
  const filters = { q: "", day: "", tier: "", rule: "", status: "" };
  const PAGE = 20;
  let page = 0, listed = [], served = true, ready = Promise.resolve();
  // The latest investigation of an account-day wins: a live one (round "live") over a recorded round.
  const caseOf = (account, day) => { const all = DATA.trace.cases.filter(c => c.account === account && c.day === day);
    return all.find(c => c.round === "live") || all[0]; };

  function activeGroups() {
    const v = DATA.versions.find(x => x.id === DATA.active_version) || DATA.versions[DATA.versions.length - 1];
    return v?.rules?.groups || [];
  }
  function gradeOf(features) {
    for (const s of DATA.showcase?.tier_agent?.segments || []) {
      const conditions = s.conditions.map(c => ({ ...c, actual: features[c.feature] }));
      if (conditions.every(c => c.actual != null && (c.op === "ge" ? c.actual >= c.value : c.actual <= c.value)))
        return { name: s.name, tier: s.tier, priority: s.priority, reason: s.reason, conditions };
    }
    return null;
  }
  function pendingCase(row) {
    const hits = activeGroups().map(g => ({ group: g.id, atoms: g.atoms.map(a => ({ ...a, value: row.features[a.feature], effective: row.features[a.feature],
      hit: row.features[a.feature] != null && (a.operator === "ge" ? row.features[a.feature] >= a.threshold : row.features[a.feature] <= a.threshold) })) }))
      .filter(g => g.atoms.every(a => a.hit));
    const grade = gradeOf(row.features) || { name: row.tier_rule || "default", tier: row.tier };
    return { key: `${row.account}|${row.day}`, investigated: false, status: row.status || "pending", error: row.error, account: row.account, day: row.day, priority: row.priority,
      rules: row.rules, tier: { ...grade, tier: row.tier }, decision: null, chain_accounts: null,
      detail: { hit_rules: hits }, evaluation: { new_chain_accounts: [] } };
  }
  const asCase = c => ({ ...c, run: c.key, key: `${c.account}|${c.day}`, investigated: true, status: "investigated" });
  const STATUS = { investigated: ["已初步调查", "Investigated"], running: ["调查中", "Investigating"], failed: ["调查失败", "Failed"], pending: ["未初步调查", "Not investigated"] };
  const statusPill = c => { const st = c.status || (c.investigated ? "investigated" : "pending");
    return `<i class="status ${st}"${c.error ? ` title="${H.esc(failure(c.error))}"` : ""}>${tr(STATUS[st])}</i>`; };
  // Plain-language reasons for the failures a live run can end with (the raw error follows).
  const FAILURES = [
    [/Aggregator decision invalid/i, ["合并判决阶段模型输出格式无效", "the merged verdict from the model was invalid"]],
    [/Planner contract invalid/i, ["调查员连续两步输出格式无效", "an investigator's output was invalid twice in a row"]],
    [/Team setup failed/i, ["组队阶段出错", "team setup failed"]],
    [/DataError|Zetaris/i, ["数据查询失败", "a data query failed"]],
    [/Worker exited/i, ["调查进程意外退出", "the investigation process exited"]],
    [/budget/i, ["超出调查预算", "the investigation budget ran out"]],
    [/stalled|no recorded progress/i, ["调查进程中断（30 分钟无进展）", "stalled (no progress for 30 minutes)"]],
  ];
  const failure = error => { const hit = FAILURES.find(([re]) => re.test(error || "")); return hit ? `${tr(hit[1])}（${error}）` : (error || L("原因未知", "unknown reason")); };
  function toast(text, kind = "") {
    const box = H.$("#toasts") || document.body.appendChild(H.el("div", "toasts")); box.id = "toasts";
    const item = H.el("div", "toast " + kind, H.esc(text)); box.appendChild(item);
    setTimeout(() => item.classList.add("out"), 7000); setTimeout(() => item.remove(), 7600);
  }
  const rowCase = row => { const c = row.case || caseOf(row.account, row.day); return c ? asCase(c) : pendingCase(row); };

  async function renderTrace() {
    const v = DATA.trace.validation;
    const wan = x => L(H.num(Math.round(x / 1e4)) + "万", (x / 1e6).toFixed(1) + "M");
    const items = [
      [L("交易量", "Transactions"), v.transactions, wan, L(`ACH/电汇 ${H.num(Math.round(v.in_scope_transactions / 1e4))} 万笔`, `ACH/Wire ${(v.in_scope_transactions / 1e6).toFixed(2)}M`), ""],
      [L("洗钱交易", "Laundering transactions"), v.laundering_transactions, x => H.num(Math.round(x)), L(`ACH/电汇 ${H.num(v.in_scope_laundering_transactions)} 笔`, `ACH/Wire ${H.num(v.in_scope_laundering_transactions)}`), "accent"],
      [L("告警量", "Alerts"), v.alerts_per_day, x => H.num(Math.round(x)) + L(" / 天", " / day"), L(`${v.days} 天共 ${(v.alerts_per_day * v.days / 1e4).toFixed(1)} 万账户日`, `${v.days} days · ${(v.alerts_per_day * v.days / 1e3).toFixed(0)}k account-days`), ""],
      [L("链路召回率", "Chain recall"), v.chain_recall, x => (x * 100).toFixed(2) + "%", L("目标 ≥ 97%", "target ≥ 97%"), "good"],
    ];
    const box = H.$("#t-kpis"); box.innerHTML = "";
    items.forEach(([label, value, format, note, cls], i) => {
      if (i) box.appendChild(H.el("div", "t-arrow", "›"));
      const k = H.el("div", "t-kpi " + cls, `<span>${label}</span><b></b><small>${H.esc(note)}</small>`);
      box.appendChild(k); countUp(H.$("b", k), value, format, 900 + i * 200);
    });
    H.$("#side-scope").innerHTML = `<span>${L("验证期", "Validation")} · ${H.esc((v.period || "").replace(/2022-/g, ""))}</span><small>${L("规则", "Rules")} ${H.esc(v.rule_version || "")} · ${L("风险分级 agent", "Risk grading agent")} ${H.esc(DATA.showcase?.tier_agent?.label || "")}</small>`;
    const summary = await fetch("/dashboard/api/alerts/summary").then(r => r.ok ? r.json() : null).catch(() => null);
    served = !!summary;
    const local = DATA.trace.cases;
    const days = summary?.days || [...new Set(local.map(c => c.day))].sort(), rules = summary?.rules || [...new Set(local.flatMap(c => c.rules))].sort();
    const sel = (name, icon, label, list, show = x => x) => `<label class="t-sel"><i>${icon}</i><select data-f="${name}"><option value="">${label}</option>${list.map(x => `<option value="${H.esc(x)}"${filters[name] === x ? " selected" : ""}>${H.esc(show(x))}</option>`).join("")}</select></label>`;
    H.$("#t-filters").innerHTML = `<label class="t-search"><i>⌕</i><input data-f="q" value="${H.esc(filters.q)}" placeholder="${L("搜索账户", "Search account")}"></label>
      ${sel("day", "▦", L(`日期：${days[0]?.slice(5)} — ${days[days.length - 1]?.slice(5)}`, `Date: ${days[0]?.slice(5)} — ${days[days.length - 1]?.slice(5)}`), days)}
      ${sel("tier", "≡", L("风险等级：全部", "Risk level: all"), ["high", "mid", "low"], x => tr(TIER[x]))}
      ${sel("status", "◐", L("调查状态：全部", "Status: all"), ["investigated", "running", "failed", "pending"], x => tr(STATUS[x]))}
      ${sel("rule", "⑂", L("命中规则：全部", "Hit rule: all"), rules)}
      <span class="t-count" id="t-count"></span>`;
    let typing = null;
    H.$$("[data-f]", H.$("#t-filters")).forEach(input => input.addEventListener("input", () => {
      filters[input.dataset.f] = input.value.trim(); page = 0;
      clearTimeout(typing); typing = setTimeout(renderRows, input.dataset.f === "q" ? 300 : 0);
    }));
    await renderRows();
  }

  async function fetchPage() {
    if (served) {
      const query = new URLSearchParams({ page, ...filters });
      const body = await fetch(`/dashboard/api/alerts?${query}`).then(r => r.ok ? r.json() : null).catch(() => null);
      if (body) return { total: body.total, pages: body.pages, rows: body.rows.map(rowCase) };
      served = false;
    }
    // No alert index on this server (static hosting): only the investigated cases exported in data.json.
    const rank = { high: 0, mid: 1, low: 2 };
    const all = DATA.trace.cases.map(asCase)
      .filter(c => (!filters.q || c.account.includes(filters.q)) && (!filters.day || c.day === filters.day) && (!filters.tier || c.tier.tier === filters.tier)
        && (!filters.rule || c.rules.includes(filters.rule)) && (!filters.status || filters.status === "investigated"))
      .sort((a, b) => (rank[a.tier.tier] ?? 3) - (rank[b.tier.tier] ?? 3));
    return { total: all.length, pages: Math.max(1, Math.ceil(all.length / PAGE)), rows: all.slice(page * PAGE, (page + 1) * PAGE) };
  }

  async function renderRows() {
    const { total, pages, rows } = await fetchPage();
    listed = rows;
    H.$("#t-count").textContent = L(`共 ${H.num(total)} 条告警 · 已调查 ${DATA.trace.cases.length}`, `${H.num(total)} alerts · ${DATA.trace.cases.length} investigated`);
    const head = `<div class="t-row t-head"><span>${L("账户", "Account")}</span><span>${L("命中规则", "Hit rules")}</span><span>${L("告警等级", "Risk level")}</span><span>${L("调查状态", "Status")}</span><span>${L("判决", "Decision")}</span><span>${L("链路账户", "Chain accounts")}</span><span>${L("时间", "Date")}</span></div>`;
    H.$("#t-table").innerHTML = head + (rows.length ? rows.map((c, i) => `
      <div class="t-row${current?.key === c.key ? " on" : ""}" data-key="${H.esc(c.key)}" style="animation-delay:${Math.min(i, 20) * 25}ms">
        <span class="mono">${H.esc(c.account)}${c.evaluation.new_chain_accounts.length ? `<em class="t-new" title="${L("调查补全了规则未告警的链路账户", "The team found chain accounts the rule never alerted on")}">+${c.evaluation.new_chain_accounts.length} ${L("新发现", "found")}</em>` : ""}</span>
        <span class="t-rules">${c.tier.conditions ? `<b>${H.esc(tierTitle(c.tier))}</b>` : `<b>${H.esc(c.rules.slice(0, 2).join(" · "))}</b>`}<small class="mono">${c.rules.length ? "+" + c.rules.length : ""}</small></span>
        <span><i class="pill tier-${c.tier.tier}">${tr(TIER[c.tier.tier]) || "—"}</i></span>
        <span>${statusPill(c)}</span>
        <span>${c.decision ? `<i class="pill dec-${c.decision}">${tr(DECISION[c.decision])}</i>` : `<span class="dim">—</span>`}</span>
        <span class="mono">${c.chain_accounts ?? "—"}</span>
        <span class="mono dim">${H.esc(c.day)}</span>
      </div>`).join("") : `<div class="t-empty">${L("没有符合条件的告警", "No alerts match")}</div>`)
      + `<div class="t-pager"><button data-p="${page - 1}"${page ? "" : " disabled"}>‹</button>
        ${pagerNumbers(page, pages).map(p => p === "…" ? `<span>…</span>` : `<button data-p="${p}" class="${p === page ? "on" : ""}">${p + 1}</button>`).join("")}
        <button data-p="${page + 1}"${page < pages - 1 ? "" : " disabled"}>›</button><small>${L(`第 ${page + 1} / ${H.num(pages)} 页 · 每页 ${PAGE} 条`, `page ${page + 1} of ${H.num(pages)} · ${PAGE} per page`)}</small></div>`;
    H.$$(".t-row[data-key]", H.$("#t-table")).forEach(r => r.addEventListener("click", () => open(r.dataset.key)));
    H.$$(".t-pager button[data-p]", H.$("#t-table")).forEach(b => b.addEventListener("click", () => { page = +b.dataset.p; renderRows(); H.$("#t-table").scrollIntoView({ block: "start", behavior: "smooth" }); }));
  }

  function pagerNumbers(cur, total) {
    const keep = new Set([0, total - 1, cur - 1, cur, cur + 1].filter(p => p >= 0 && p < total));
    const out = []; let last = -1;
    [...keep].sort((a, b) => a - b).forEach(p => { if (p - last > 1) out.push("…"); out.push(p); last = p; });
    return out;
  }

  /* ---------- drawer ---------- */
  function close() {
    const wasOpen = H.$("#drawer").classList.contains("on");
    H.$("#drawer").classList.remove("on"); H.$("#scrim").classList.remove("on");
    stop(); clearTimeout(liveTimer); current = null; H.$$(".t-row.on").forEach(r => r.classList.remove("on"));
    if (wasOpen) renderRows();  // statuses may have changed (an investigation started or finished in the drawer)
  }

  function open(key) {
    const found = listed.find(x => x.key === key) || (() => { const c = DATA.trace.cases.find(x => x.key === key || x.key.startsWith(key)); return c ? asCase(c) : null; })();
    if (!found) return;
    const c = found;
    current = c; stop(); clearTimeout(liveTimer);
    H.$$(".t-row").forEach(r => r.classList.toggle("on", r.dataset.key === c.key));
    const d = c.detail, drawer = H.$("#drawer");
    drawer.innerHTML = `
      <div class="d-head"><div class="d-title"><h3 class="mono">${H.esc(c.account)}</h3>
        <div class="d-meta"><i class="pill tier-${c.tier.tier}">${tr(TIER[c.tier.tier]) || "—"}${c.tier.conditions ? " · " + H.esc((c.tier.name || "").split("_")[0]) : ""}</i>
        <span id="d-status">${statusPill(c)}</span>
        ${c.decision ? `<i class="pill dec-${c.decision} solid">${tr(DECISION[c.decision])}</i>` : ""}
        <span>${H.esc(c.day)} · ${L("验证期", "validation")}${c.investigated ? ` · ${d.model_calls} ${L("次模型调用", "model calls")} · ${d.sql_requests} ${L("次 Zetaris 查询", "Zetaris queries")}` : ""}</span></div></div>
        <div class="d-actions"><button class="btn-solid" id="d-live"><i>⚡</i>${L("立刻调查此告警", "Investigate this alert now")}</button><button class="d-close" title="${L("关闭", "Close")}">✕</button></div></div>
      <div class="d-row top">
        <section class="d-sec d-rules-sec"><h4>① ${L("命中规则", "Hit rules")} <small>${L("只列命中的规则组", "Only the rule groups that hit")}</small></h4><div id="d-rules"></div></section>
        <section class="d-sec"><h4>② ${L("风险分级原因", "Why this risk level")} <small>${L("风险分级 agent 为什么这样分级", "Why the risk grading agent placed it here")}</small></h4><div id="d-reason"></div></section>
      </div>
      ${c.investigated ? `<div class="player"><button id="p-play">❚❚</button><button id="p-restart" title="${L("重新开始", "Restart")}">↺</button>
        <div class="p-track" id="p-track"><i id="p-bar"></i></div><span id="p-step"></span><button id="p-speed">1×</button></div>
      <div class="d-row bottom">
        <section class="d-sec d-net-sec"><h4>③ ${L("关系网追查", "Network tracing")} <small>${L("跟随回放逐步追寻 · 每名调查员一种颜色，可单独查看", "Unfolds with the replay · one colour per investigator, view each alone")}</small></h4>
          <div class="d-tabs" id="d-tabs"></div><div class="d-net"><svg id="d-net"></svg></div></section>
        <section class="d-sec d-replay-sec"><h4>④ ${L("调查回放", "Investigation replay")} <small>${L("每一步同步点亮关系网 · reasoning 逐字展开", "Each step lights up the network · reasoning types out")}</small></h4><div id="d-replay"></div></section>
      </div>
      <section class="d-sec"><h4>⑤ ${L("账户判定", "Account verdicts")} <small>${L("调查团队对案件中每个账户的结论：可疑、已排除、证据不足，或尚未专门调查", "The team's conclusion on every account in the case")}</small></h4><div id="d-accounts"></div></section>` : ""}
      <section class="d-live" id="d-live-sec"></section>`;
    H.$(".d-close", drawer).addEventListener("click", close);
    H.$("#d-live", drawer).addEventListener("click", () => startLive(c));
    drawer.classList.add("on"); H.$("#scrim").classList.add("on"); drawer.scrollTop = 0;
    renderHitRules(c); renderReason(c);
    if (c.investigated) renderAccounts(c);
    resumeLive(c);
    if (!c.investigated) return;
    c._filter = null; renderTabs(c);
    startReplay(c);
  }

  function renderAccounts(c) {
    const accounts = c.detail.accounts || [], fresh = new Set(c.evaluation.new_chain_accounts);
    const kinds = ["suspicious", "cleared", "insufficient", "not_examined"].filter(k => accounts.some(a => a.verdict === k));
    H.$("#d-accounts").innerHTML = `<div class="a-sum">${kinds.map(k => `<span class="av ${k}"><b>${accounts.filter(a => a.verdict === k).length}</b> ${tr(ACCOUNT[k])}<small>${H.esc(tr(ACCOUNT[k].slice(2)))}</small></span>`).join("")}</div>
      <div class="a-table"><div class="a-row a-head"><span>${L("账户", "Account")}</span><span>${L("位置", "Position")}</span><span>${L("判定", "Verdict")}</span><span>${L("团队给出的理由", "Team's reason")}</span><span>${L("查过它的调查员 / 工具", "Examined by / tools")}</span></div>
      ${accounts.map(a => `<div class="a-row">
        <span class="mono">${H.esc(a.account)}${fresh.has(a.account) ? `<em class="t-new">${L("新发现", "found")}</em>` : ""}</span>
        <span>${tr(POSITION[a.role]) || "—"}</span>
        <span><i class="av ${a.verdict}">${tr(ACCOUNT[a.verdict]) || a.verdict}</i></span>
        <span class="a-why">${H.esc(a.reason || (a.verdict === "not_examined" ? L("团队没有对它下结论", "no verdict from the team") : "—"))}</span>
        <span class="a-tools">${a.found_by?.length ? H.esc([...new Set(a.found_by.map(r => r === "预取" ? L("告警上下文预取", "alert context prefetch") : role(r)))].join(L("、", ", "))) : "—"}${a.tools?.length ? `<small>${H.esc(a.tools.map(tool).join(" · "))}</small>` : ""}</span>
      </div>`).join("")}</div>`;
  }

  function renderHitRules(c) {
    const box = H.$("#d-rules");
    if (!c.detail.hit_rules.length) { box.innerHTML = `<div class="dim">${L("该告警的规则命中明细未记录", "Rule hit details were not recorded")}</div>`; return; }
    box.innerHTML = c.detail.hit_rules.map(g => `<div class="d-rule"><b>${H.esc(g.group)}</b>${g.atoms.map(a => {
      const val = a.effective ?? a.value ?? 0, top = Math.max(a.threshold * 2, val * 1.15, 1e-6);
      return `<div class="d-atom" data-feature="${H.esc(a.feature)}"><span>${H.esc(fname(a.feature))}</span>
        <div class="bar"><i data-w="${Math.max(2, Math.min(100, val / top * 100))}%"></i></div>
        <small class="mono">${L("阈值", "threshold")} ${opx(a.operator)} ${H.fmt(a.threshold)}</small><b class="mono">${H.fmt(val)}</b></div>`;
    }).join("")}</div>`).join("");
    H.$$(".d-atom", box).forEach(n => n.addEventListener("click", () => H.featureModal(n.dataset.feature)));
    grow(box);
  }

  function renderReason(c) {
    const box = H.$("#d-reason"), t = c.tier;
    if (!t.conditions) {
      box.innerHTML = `<div class="r-verdict"><div><small>${L("风险等级", "Risk level")}</small><b class="tier-${t.tier}">${tr(TIER[t.tier]) || "—"}</b></div>
        <div><small>${L("命中规则", "Matched rule")}</small><span>${L("未命中任何高 / 中风险模式，归入默认等级", "No high or medium pattern matched; default level")}</span></div></div>`;
      return;
    }
    const s = segmentStats(t.name);
    const facts = [
      [L("命中规则", "Matched rule"), L(`${t.name.split("_")[0]} · 优先级 ${t.priority} · ${s.rank}/${s.total}`, `${t.name.split("_")[0]} · priority ${t.priority} · ${s.rank}/${s.total}`)],
      s.own ? [L("开发集该规则", "This rule on development"), L(`${s.own.alerts} 告警 · ${s.own.rate}% 洗钱`, `${s.own.alerts} alerts · ${s.own.rate}% laundering`)] : null,
      s.pattern ? [L("来源模式", "Source pattern"), L(`挖掘模式 · 精度 ${pctx(s.pattern.precision)}`, `Mined pattern · precision ${pctx(s.pattern.precision)}`)] : null,
    ].filter(Boolean);
    box.innerHTML = `
      <div class="r-verdict"><div><small>${L("风险等级", "Risk level")}</small><b class="tier-${t.tier}">${tr(TIER[t.tier])}</b></div>
        ${facts.map(([k, v]) => `<div><small>${k}</small><span>${H.esc(v)}</span></div>`).join("")}</div>
      <div class="r-title">${H.esc(tierTitle(t))} — ${L(`${t.conditions.length} 个条件全部成立`, `all ${t.conditions.length} conditions hold`)}</div>
      ${t.conditions.map(cd => {
        const top = cd.op === "ge" ? Math.max(cd.value * 2, cd.actual * 1.1, 1e-6) : Math.max(cd.value * 1.5, cd.actual * 1.1, 1e-6);
        return `<div class="r-cond"><i>✓</i><div class="r-ctext"><b>${H.esc(fname(cd.feature))}</b><small>${H.esc(why(cd))}</small></div>
          <div class="bar accent"><i data-w="${Math.max(2, Math.min(100, cd.actual / top * 100))}%"></i></div>
          <small class="mono">${opx(cd.op)} ${H.fmt(cd.value)}</small><b class="mono">${H.fmt(cd.actual)}</b></div>`;
      }).join("")}
      <div class="r-basis"><span>${L("分级依据", "Grading basis")}</span><p>${H.esc(t.reason || "")}</p>
        ${s.own && s.unmatched ? `<small>${L(`第 ${s.round} 轮学习时把它放在这一级：命中这条规则的开发集告警 ${s.own.rate}% 是洗钱，没有命中任何分级规则的告警只有 ${s.unmatched.rate}%。`, `Placed here in round ${s.round}: ${s.own.rate}% of development alerts matching this rule are laundering, against ${s.unmatched.rate}% for alerts matching no grading rule.`)}</small>` : ""}</div>`;
    grow(box);
  }

  /* ---------- network ---------- */
  // Each case draws into its own svg and tabs (the replay and the live view share the drawer) with its own filter.
  function renderTabs(c) {
    const tabs = [[L("全部", "All"), null, null], ...c.detail.members.map((m, i) => [role(m.role), i, MEMBER[i % MEMBER.length]])];
    const box = H.$(c._tabs || "#d-tabs"); if (!box) return;
    box.innerHTML = tabs.map(([l, i, col]) => `<button class="${(c._filter ?? null) === i ? "on" : ""}" data-m="${i ?? ""}">${col ? `<i style="background:${col}"></i>` : ""}${H.esc(l)}</button>`).join("");
    H.$$("button", box).forEach(b => b.addEventListener("click", () => { c._filter = b.dataset.m === "" ? null : +b.dataset.m; renderTabs(c); drawNet(c, c._step ?? Infinity, false); }));
  }

  function layout(c) {
    const d = c.detail, W = 500, Hh = 330, focus = c.account;
    const roles = Object.fromEntries(d.nodes.map(n => [n.id, n.role]));
    const ranked = d.edges.filter(e => e.from !== e.to).sort((a, b) => (b.step > 0) - (a.step > 0) || (b.verdict === "confirm") - (a.verdict === "confirm"));
    const edges = ranked.slice(0, 30), ids = new Set([focus]);
    edges.forEach(e => { ids.add(e.from); ids.add(e.to); });
    const groups = { upstream: [], downstream: [], peer: [] };
    [...ids].filter(id => id !== focus).forEach(id => (groups[roles[id]] || groups.peer).push(id));
    const pos = { [focus]: [W / 2, Hh / 2] };
    const column = (list, x) => list.forEach((id, i) => { pos[id] = [x, 46 + (Hh - 100) * (list.length === 1 ? 0.5 : i / (list.length - 1))]; });
    column(groups.upstream.slice(0, 8), 70); column(groups.downstream.slice(0, 8), W - 70);
    groups.peer.slice(0, 6).forEach((id, i, list) => { pos[id] = [W / 2 - 110 + 220 * (list.length === 1 ? 0.5 : i / (list.length - 1)), i % 2 ? Hh - 46 : 30]; });
    return { W, Hh, pos, edges: edges.filter(e => pos[e.from] && pos[e.to]) };
  }

  function drawNet(c, step, animate = true) {
    c._step = step;
    const svg = H.$(c._svg || "#d-net"); if (!svg) return;
    const L2 = c._layout || (c._layout = layout(c)), d = c.detail;
    svg.setAttribute("viewBox", `0 0 ${L2.W} ${L2.Hh}`);
    const fresh = new Set(c.evaluation.new_chain_accounts), suspects = new Set(d.merge?.suspect_transaction_ids || []);
    const filter = c._filter ?? null;
    const visible = L2.edges.filter(e => e.step <= step && (filter === null || e.member === filter));
    const shown = new Set([c.account]); visible.forEach(e => { shown.add(e.from); shown.add(e.to); });
    const prev = svg._shown || new Set();
    svg.innerHTML = "";
    const defs = H.svgEl("defs"), marker = H.svgEl("marker", { id: "arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 5, markerHeight: 5, orient: "auto-start-reverse" });
    marker.appendChild(H.svgEl("path", { d: "M0,0 L10,5 L0,10 z", fill: "#9a9483" })); defs.appendChild(marker); svg.appendChild(defs);
    const pairs = {}, stepDots = [];
    visible.forEach(e => {
      const key = [e.from, e.to].sort().join("|"), n = (pairs[key] = (pairs[key] || 0) + 1);
      const [x1, y1] = L2.pos[e.from], [x2, y2] = L2.pos[e.to], bend = 16 * n * (n % 2 ? 1 : -1);
      const mx = (x1 + x2) / 2 - (y2 - y1) * bend / 200, my = (y1 + y2) / 2 + (x2 - x1) * bend / 200;
      const color = e.member == null ? "#c9bda4" : MEMBER[e.member % MEMBER.length], hot = suspects.has(e.id);
      const path = H.svgEl("path", { d: `M${x1},${y1} Q${mx},${my} ${x2},${y2}`, fill: "none", stroke: color, "stroke-width": hot ? 3 : 1.6,
        "stroke-dasharray": e.member == null ? "4 4" : "", "marker-end": "url(#arrow)", class: "net-edge" });
      if (animate && !prev.has(e.id) && e.member != null) {
        path.style.strokeDasharray = 700; path.style.strokeDashoffset = 700;
        requestAnimationFrame(() => { path.style.transition = "stroke-dashoffset 1.1s ease"; path.style.strokeDashoffset = 0; });
      }
      path.addEventListener("mousemove", ev => H.showTip(ev, `<b>${H.esc(e.id)}</b><br>${H.esc(e.from)} → ${H.esc(e.to)}<br>${H.esc(String(e.amount))} ${H.esc(e.currency || "")} · ${H.esc(e.time || "")}<br>${e.member == null ? L("告警上下文预取", "Prefetched alert context") : L(`第 ${e.step} 步 · ${role(d.members[e.member]?.role)} 发现`, `Step ${e.step} · found by ${role(d.members[e.member]?.role)}`)}${hot ? `<br><b>${L("合并判决：可疑交易", "Merged verdict: suspect")}</b>` : ""}`));
      path.addEventListener("mouseleave", H.hideTip);
      svg.appendChild(path);
      if (e.member != null && e.step) stepDots.push([(x1 + 2 * mx + x2) / 4, (y1 + 2 * my + y2) / 4, e.step, color]);
      if (hot) { const t = H.svgEl("text", { x: (x1 + 2 * mx + x2) / 4, y: (y1 + 2 * my + y2) / 4 - 9, class: "net-amount", "text-anchor": "middle" }); t.textContent = `${H.num(Math.round(+e.amount))} ${e.currency?.split(" ")[0] || ""}`; svg.appendChild(t); }
    });
    Object.entries(L2.pos).forEach(([id, [x, y]]) => {
      if (!shown.has(id)) return;
      const isFocus = id === c.account, isNew = fresh.has(id), verdict = d.accounts.find(a => a.account === id)?.verdict;
      const g = H.svgEl("g", { class: "net-node" + (!prev.has(id) && animate ? " pop" : ""), transform: `translate(${x},${y})` });
      g.appendChild(H.svgEl("circle", { r: isFocus ? 22 : 14, fill: isFocus ? "var(--accent)" : "#ffffff", stroke: isNew || isFocus || verdict === "suspicious" ? "var(--accent)" : "#c9bda4", "stroke-width": isNew ? 2.6 : 1.6 }));
      if (isNew) g.appendChild(H.svgEl("circle", { r: 20, fill: "none", stroke: "var(--accent)", "stroke-width": 1.2, class: "net-pulse" }));
      const label = H.svgEl("text", { y: isFocus ? 36 : 28, "text-anchor": "middle", class: "net-label" + (isNew ? " new" : "") });
      label.textContent = short(id);
      const sub = H.svgEl("text", { y: isFocus ? 49 : 41, "text-anchor": "middle", class: "net-sub" });
      sub.textContent = isFocus ? L("告警账户", "alerted") : isNew ? L("新发现 · 未告警", "found · never alerted") : verdict ? tr(ACCOUNT[verdict]?.slice(0, 2)) || verdict : L("关联", "linked");
      g.appendChild(label); g.appendChild(sub);
      g.addEventListener("mousemove", ev => H.showTip(ev, `<b>${H.esc(id)}</b><br>${isFocus ? L("告警账户", "Alerted account") : isNew ? L("调查补全的链路账户（规则从未告警）", "Chain account found by the team (never alerted)") : L("关联账户", "Linked account")}`));
      g.addEventListener("mouseleave", H.hideTip);
      svg.appendChild(g);
    });
    stepDots.forEach(([x, y, n, col]) => { const g = H.svgEl("g", { transform: `translate(${x},${y})`, class: "net-step" }); g.appendChild(H.svgEl("circle", { r: 9, fill: col })); const t = H.svgEl("text", { y: 3.5, "text-anchor": "middle" }); t.textContent = n; g.appendChild(t); svg.appendChild(g); });
    svg._shown = new Set([...visible.map(e => e.id), ...shown]);
  }

  /* ---------- replay ---------- */
  const stages = c => [{ kind: "plan" }, ...c.detail.steps.map(s => ({ kind: "step", s })), { kind: "merge" }, { kind: "senior" }];
  function stop() { if (player) { clearTimeout(player.timer); player.playing = false; } }

  function startReplay(c) {
    player = { c, list: stages(c), at: 0, playing: true, speed: 1, timer: null };
    H.$("#p-play").addEventListener("click", () => { player.playing = !player.playing; H.$("#p-play").textContent = player.playing ? "❚❚" : "▶"; if (player.playing) { if (player.at >= player.list.length - 1) { player.at = -1; H.$("#d-replay").innerHTML = ""; H.$("#d-net")._shown = new Set(); } advance(); } else clearTimeout(player.timer); });
    H.$("#p-restart").addEventListener("click", () => { clearTimeout(player.timer); player.at = 0; player.playing = true; H.$("#p-play").textContent = "❚❚"; H.$("#d-net")._shown = new Set(); H.$("#d-replay").innerHTML = ""; paint(true); schedule(); });
    H.$("#p-speed").addEventListener("click", () => { player.speed = player.speed === 1 ? 2 : player.speed === 2 ? 0.5 : 1; H.$("#p-speed").textContent = player.speed + "×"; });
    H.$("#p-track").addEventListener("click", ev => { const r = ev.currentTarget.getBoundingClientRect(); player.at = Math.round((ev.clientX - r.left) / r.width * (player.list.length - 1)); paint(false); schedule(); });
    paint(true); schedule();
  }
  function schedule() {
    clearTimeout(player.timer);
    if (player.playing && player.at < player.list.length - 1) player.timer = setTimeout(advance, 2800 / player.speed);
    else if (player.at >= player.list.length - 1) { player.playing = false; H.$("#p-play").textContent = "▶"; }
  }
  function advance() { player.at += 1; paint(true); schedule(); }

  // The replay is a timeline: entries are appended in drawing order and stay; the feed scrolls up as it grows.
  function entry(c, e, live) {
    const d = c.detail, merge = d.merge || {}, senior = d.senior || {};
    const reason = text => text ? `<div class="r-reason${live ? " live" : ""}"><span>${live ? L("REASONING · 生成中", "REASONING · live") : "REASONING"}</span><p${live ? ` data-tw="1" data-text="${H.esc(text)}"` : ""}>${live ? "" : H.esc(text)}</p></div>` : "";
    if (e.kind === "plan") return `<div class="r-entry plan" style="--mc:var(--primary)"><i class="r-dot">◎</i><div class="r-body">
      <div class="r-line"><b>${L("高级调查员分工", "Senior investigator assigns")}</b><small>Nemotron Ultra · ${L(`${d.members.length} 名调查员`, `${d.members.length} investigators`)}</small></div>
      <div class="r-team">${d.members.map((m, i) => `<span style="--mc:${MEMBER[i % MEMBER.length]}"><b>${H.esc(role(m.role))}</b>${H.esc((m.question || "").slice(0, 90))}</span>`).join("")}</div>
      ${reason(d.team_plan)}</div></div>`;
    if (e.kind === "step") {
      const s = e.s, m = d.members[s.member] || {}, col = MEMBER[s.member % MEMBER.length];
      return `<div class="r-entry" style="--mc:${col}"><i class="r-dot">${s.step}</i><div class="r-body">
        <div class="r-line"><b style="color:${col}">${H.esc(role(m.role))}</b><code>${H.esc(s.tool)}</code><small>${H.esc(tool(s.tool))}</small></div>
        ${reason(s.reasoning)}
        ${s.found.slice(0, 3).map(f => `<div class="r-found">· ${H.esc(String(f).slice(0, 160))}</div>`).join("")}
        ${s.transactions?.length ? `<div class="r-tx">${L("关系网新增交易", "New on the network")}: ${s.transactions.map(t => `<code>${H.esc(t)}</code>`).join(" ")}</div>` : ""}</div></div>`;
    }
    if (e.kind === "merge") return `<div class="r-entry" style="--mc:var(--ag2)"><i class="r-dot">⊕</i><div class="r-body">
      <div class="r-line"><b>${L("合并判决", "Merged verdict")}</b><small>${H.esc(tr(VERDICT[merge.conclusion]) || merge.conclusion || "")}</small></div>
      <div class="r-concls">${d.members.map((m, i) => m.brief_reason ? `<div style="--mc:${MEMBER[i % MEMBER.length]}"><b>${H.esc(role(m.role))}</b> ${H.esc(tr(VERDICT[m.conclusion]) || "")} · ${H.esc(m.brief_reason.slice(0, 140))}…</div>` : "").join("")}</div>
      ${reason(merge.brief_reason)}</div></div>`;
    const flags = senior.evidence_flags || [];
    return `<div class="r-entry senior" style="--mc:var(--accent)"><i class="r-dot">⚖</i><div class="r-body">
      <div class="r-line"><b>${L("高级调查员研判", "Senior investigator decides")}</b><small>${senior.decision ? `${L("先验", "prior")} ${H.esc(senior.prior_level)} → ${H.esc(senior.risk_level)} · ${tr(DECISION[senior.decision])}` : L("未研判", "pending")}</small></div>
      ${flags.length ? `<div class="r-flags">${flags.map(f => `<span class="${ORDINARY.has(f.flag) ? "ok" : ""}">${H.esc(tr(FLAG[f.flag]) || f.flag)} · ${H.esc(f.evidence_ref)}</span>`).join("")}</div>` : ""}
      ${reason(senior.rationale)}
      ${c.evaluation.new_chain_accounts.length ? `<div class="r-new"><i>⌕</i><div>${L("补全漏放账户", "Missed account recovered")} ${c.evaluation.new_chain_accounts.map(a => `<b class="mono">${H.esc(a)}</b>`).join("、")} —— ${L("规则从未告警，由调查团队在链路上找到", "never alerted by the rules; found on the chain by the team")}</div></div>` : ""}</div></div>`;
  }

  function paint(animate) {
    const { c, list, at } = player, cur = list[at];
    const stepNo = cur.kind === "plan" ? 0 : cur.kind === "step" ? cur.s.step : Infinity;
    H.$("#p-bar").style.width = (at / Math.max(1, list.length - 1) * 100) + "%";
    H.$("#p-step").textContent = L(`调查回放 · 步骤 ${at + 1} / ${list.length}`, `Replay · step ${at + 1} / ${list.length}`);
    drawNet(c, stepNo, animate);
    let feed = H.$("#r-feed");
    if (!feed || feed._case !== c.key) { H.$("#d-replay").innerHTML = `<div class="r-feed" id="r-feed"></div>`; feed = H.$("#r-feed"); feed._case = c.key; feed._count = 0; }
    if (feed._count > at + 1) { feed.innerHTML = ""; feed._count = 0; animate = false; }  // seeking back rebuilds the past
    for (let i = feed._count; i <= at; i++) feed.insertAdjacentHTML("beforeend", entry(c, list[i], animate && i === at));
    feed._count = at + 1;
    H.$$(".r-entry", feed).forEach((n, i) => n.classList.toggle("cur", i === at));
    // Finished entries: complete their text and drop the live styling.
    H.$$(".r-entry:not(.cur) [data-tw]", feed).forEach(p => { clearInterval(p._tw); p.textContent = p.dataset.text; p.removeAttribute("data-tw"); });
    H.$$(".r-entry:not(.cur) .r-reason.live", feed).forEach(r => { r.classList.remove("live"); r.querySelector("span").textContent = "REASONING"; });
    const follow = () => { feed.scrollTop = feed.scrollHeight; };
    const tw = H.$(".r-entry.cur [data-tw]", feed);
    if (tw) typewrite(tw, tw.dataset.text, follow);
    feed.scrollTo({ top: feed.scrollHeight, behavior: animate ? "smooth" : "auto" });
  }

  /* =================== rule evolution =================== */
  function renderEvolution() {
    const s = DATA.showcase || {}, ta = s.tier_agent || {}, ia = s.investigation_agent || {}, ev = DATA.evaluation || {};
    const loop = [["◉", L("告警", "Alert")], ["≡", L("风险分级", "Risk grading")], ["⌕", L("调查", "Investigation")], ["✓", L("证据", "Evidence")], ["✦", L("学习", "Learning")], ["⑂", L("新规则", "New rules")]];
    H.$("#evo-loop").innerHTML = loop.map(([i, t], k) => `${k ? `<span class="e-link">${k === 5 ? "↺" : "→"}</span>` : ""}<div class="e-node${k === 4 ? " hot" : ""}" style="--k:${k}"><i>${i}</i><b>${t}</b></div>`).join("");

    const rounds = ta.rounds || [], cap = r => (r.capacity || []).find(b => b.alerts_per_day === 2000) || {};
    const chosen = rounds.find(r => r.chosen) || rounds[rounds.length - 1] || {};
    const agentTiers = ev.tiering?.tier_agent?.tiers || {};
    const maxP = Math.max(...rounds.map(r => r.high?.window_precision || 0), 0.01);
    H.$("#evo-tier").innerHTML = `
      <div class="e-head"><i style="background:var(--primary)">≡</i><div><b>${L("风险分级 agent", "Risk grading agent")} ${H.esc(ta.label || "")}</b><small>${L(`把每天 ${H.num(DATA.trace?.validation?.alerts_per_day)} 条告警排成高 / 中 / 低，并给出优先级`, `Grades ${H.num(DATA.trace?.validation?.alerts_per_day)} alerts a day into high / medium / low with a priority`)}</small></div></div>
      <div class="e-result"><span>${L(`选中 R${chosen.round ?? "—"} · 验证期效果`, `Chosen R${chosen.round ?? "—"} · validation`)}</span>
        ${["high", "mid", "low"].map(t => { const x = agentTiers[t] || {}; return `<div class="tier-${t}"><small>${L({ high: "高风险", mid: "中风险", low: "低风险" }[t], { high: "High", mid: "Medium", low: "Low" }[t])}</small><b class="mono">${H.num(Math.round(x.alerts_per_day || 0))}${L(" / 天", " / day")}</b><em class="mono">${L("链路准确率", "chain precision")} ${pctx(x.chain_precision)}</em></div>`; }).join("")}</div>
      <div class="e-kicker">${L(`${rounds.length} 轮学习 · 开发集上每轮的效果（窗口精度 = 告警当天确有洗钱交易的比例）`, `${rounds.length} learning rounds · development effect per round (window precision = alert-days with a laundering transfer)`)}</div>
      <div class="e-table tier">
        <div class="e-tr th"><span>${L("轮", "Round")}</span><span>${L("这一轮的推理与改动", "Reasoning and change")}</span><span>${L("高风险规则", "High rules")}</span><span>${L("高风险 / 天", "High / day")}</span><span>${L("高风险窗口精度", "High window precision")}</span><span>${L("前 2,000 召回", "Recall @2,000")}</span></div>
        ${rounds.map((r, i) => `<div class="e-tr${r.chosen ? " on" : ""}" style="animation-delay:${i * 120}ms"><b>R${r.round}</b>
          <span class="e-why" title="${H.esc(r.rationale)}">${H.esc(r.rationale)}</span><span class="mono">${r.high_rules}</span>
          <span class="mono">${H.num(Math.round(r.high?.alerts_per_day || 0))}</span>
          <span class="e-pbar"><i data-w="${(r.high?.window_precision || 0) / maxP * 100}%"></i><em class="mono">${pctx(r.high?.window_precision)}</em></span>
          <span class="mono">${pctx(cap(r).chain_recall)}</span></div>`).join("")}
      </div>
      ${ta.investigation_evidence?.case_library?.length ? `<div class="e-kicker">${L(`引用的调查证据 · 案件库 ${ta.investigation_evidence.case_library.length} 例`, `Investigation evidence used · ${ta.investigation_evidence.case_library.length} library cases`)}${ta.investigation_evidence.cited_in_segments.length ? L(` · 被 ${ta.investigation_evidence.cited_in_segments.length} 条分级规则引用`, ` · cited by ${ta.investigation_evidence.cited_in_segments.length} grading rules`) : ""}</div>` : ""}
      <div class="e-reason"><span>REASONING · R${chosen.round ?? ""}</span><p>${H.esc(chosen.analysis || ta.rationale || "")}</p></div>
      ${(s.tier_versions || []).length ? `<div class="e-kicker">${L("全部分级版本 · 验证期效果（每个版本在其学习时的规则版本上评估）", "All grading versions · validation (each on the rule version it was learned on)")}</div>
      <div class="e-table tv"><div class="e-tr th"><span>${L("版本", "Version")}</span><span>${L("规则版本", "Rules")}</span><span>${L("分级规则", "Segments")}</span><span>${L("高风险 / 天", "High / day")}</span><span>${L("高风险链路准确率", "High chain precision")}</span><span>${L("前 2,000 召回", "Recall @2,000")}</span><span></span></div>
        ${s.tier_versions.map(t => { const c2 = (t.capacity || []).find(b => b.alerts_per_day === 2000) || {};
          return `<div class="e-tr${t.primary ? " on" : ""}"><b class="mono">${H.esc(t.label)}</b><span class="mono">${H.esc(t.rule_version || "—")}</span><span class="mono">${t.segments}</span>
            <span class="mono">${t.tiers.high.alerts_per_day != null ? H.num(Math.round(t.tiers.high.alerts_per_day)) : "—"}</span><span class="mono">${pctx(t.tiers.high.chain_precision)}</span><span class="mono">${pctx(c2.chain_recall)}</span>
            <span>${t.primary ? `<i class="pill tier-low">${L("生效", "live")}</i>` : ""}</span></div>`; }).join("")}</div>` : ""}`;

    const ir = ia.rounds || [], tools = (ia.tools || []).filter(t => t.uses >= 5);
    const best = ir.reduce((b, r) => (r.chain_accounts_found / (r.chain_accounts_in_files || 1)) > (b ? b.chain_accounts_found / (b.chain_accounts_in_files || 1) : -1) ? r : b, null);
    H.$("#evo-inv").innerHTML = `
      <div class="e-head"><i style="background:${MEMBER[1]}">⌕</i><div><b>${L("调查 agent", "Investigation agent")}</b><small>${L("高级调查员分工 → 三名调查员 → 合并 → 研判", "Senior investigator assigns → three investigators → merge → decide")}</small></div></div>
      <div class="e-kicker">${L(`${ir.length} 轮验证 · 每轮改了什么，误报排除与链路补全如何变化`, `${ir.length} validation rounds · what changed, false-alarm exclusion and chain completion`)}</div>
      <div class="e-table inv">
        <div class="e-tr th"><span>${L("轮", "Round")}</span><span>${L("这一轮改了什么", "What changed")}</span><span>${L("误报排除", "False alarms cleared")}</span><span>${L("链路补全率", "Chain completion")}</span></div>
        ${ir.map((r, i) => { const cp = r.chain_accounts_in_files ? r.chain_accounts_found / r.chain_accounts_in_files : null; return `<div class="e-tr${r === best ? " on" : ""}" style="animation-delay:${i * 120}ms"><b class="mono">${H.esc(r.round.replace("-adj", ""))}</b><span>${H.esc(L(r.change, r.change_en))}</span>
          <span class="e-mbar"><div><i data-w="${(r.exclusion_rate || 0) * 100}%" style="background:var(--primary)"></i></div><em class="mono">${pctx(r.exclusion_rate)}</em></span>
          <span class="e-mbar"><div><i data-w="${(cp || 0) * 100}%"></i></div><em class="mono">${pctx(cp)} · ${r.chain_accounts_found}/${r.chain_accounts_in_files}</em></span></div>`; }).join("")}
      </div>
      <div class="e-note">${L("链路补全率 = 团队找出的链路账户 / 案件文件中出现的链路账户（事后用标签核验）。", "Chain completion = chain accounts the team named / chain accounts present in the case files (checked against labels afterwards).")}</div>
      <div class="e-kicker row">${L(`工具有效性 · 开发集洗钱案件中查到洗钱转账的比例${ia.trained_on ? `（${H.esc(ia.trained_on)}）` : ""}`, `Tool effectiveness · share of uses in laundering cases that found a laundering transfer${ia.trained_on ? ` (${H.esc(ia.trained_on)})` : ""}`)}
        <button class="e-toggle" id="tool-design">🔧 ${L("工具设计", "Tool design")} <b>▾</b></button></div>
      ${tools.map(t => `<div class="e-tool"><span>${H.esc(tool(t.tool))}</span><div><i data-w="${(t.rate || 0) * 100}%"></i></div><small class="mono">${t.found}/${t.uses}</small><b class="mono">${Math.round((t.rate || 0) * 100)}%</b></div>`).join("")}
      <div class="e-design" id="tool-design-pane">${Object.entries(TOOLS).map(([id, x]) => { const t = (ia.tools || []).find(y => y.tool === id);
        return `<div class="e-dtool"><div><b>${L(x[0], x[1])}</b><code>${id}</code><span>${t ? L("命中率 ", "hit rate ") + Math.round((t.rate || 0) * 100) + "%" : id === "get_alert_context" ? L("预取", "prefetched") : "—"}</span></div>
          <p>${L("问题：", "Question: ")}${H.esc(L(x[2], x[3]))}</p><small>${L("参数：", "Arguments: ")}${H.esc(L(x[4], x[5]))}　·　${L("返回：", "Returns: ")}${H.esc(L(x[6], x[7]))}</small></div>`; }).join("")}</div>
      <div class="e-reason inv"><span>${L("LEARNED · 写回调查团队", "LEARNED · written back to the team")}</span><p>${tools.length > 1 ? L(`${tool(tools[0].tool)} 与 ${tool(tools[1].tool)} 最能找到洗钱转账；${tools.filter(t => !t.rate).map(t => tool(t.tool)).join("、") || "其余工具"} 单独使用时找不到，只作补充。这张表写回调查团队，作为选工具的依据。`, `${tool(tools[0].tool)} and ${tool(tools[1].tool)} find laundering transfers most often; ${tools.filter(t => !t.rate).map(t => tool(t.tool)).join(", ") || "the others"} find none on their own and only supplement. This table is written back to the team to guide tool choice.`) : ""}</p></div>`;
    H.$("#tool-design").addEventListener("click", () => { const p = H.$("#tool-design-pane"); p.classList.toggle("on"); H.$("#tool-design b").textContent = p.classList.contains("on") ? "▴" : "▾"; });

    const versions = (s.rule_versions || []).filter(v => v.chain_recall >= 0.97), updates = Object.fromEntries((s.rule_updates || []).map(u => [u.label, u]));
    const maxA = Math.max(...versions.map(v => v.flagged_per_day), 1), proposals = (ev.feature_proposals || []).slice(-1)[0]?.proposals || [];
    H.$("#evo-chain").innerHTML = `
      <div class="e-head"><i style="background:var(--accent)">✦</i><div><b>${L("规则更新 agent × 数据变化链条", "Rule update agent × the data chain")}</b><small>${L("每个版本由谁做出、依据什么、改了什么，以及验证期效果（召回始终 ≥ 97%）", "Who made each version, on what evidence, what changed, and the validation effect (recall always ≥ 97%)")}</small></div></div>
      <div class="e-versions">${versions.map((v, i) => {
        const u = updates[v.label], prev = versions[i - 1], drop = prev ? (v.flagged_per_day / prev.flagged_per_day - 1) : null;
        return `<div class="e-ver${u ? " agent" : ""}" style="--d:${i * 260}ms">
          <div class="e-bararea"><small class="mono">${H.num(v.flagged_per_day)}${L(" / 天", " / day")}</small><i style="--h:${v.flagged_per_day / maxA * 130}px"></i></div>
          <div class="e-vl"><b class="mono">${H.esc(v.label)}</b>${drop != null ? `<em>${L("告警", "alerts")} ${(drop * 100).toFixed(0)}%</em>` : ""}</div>
          <div class="e-vm"><span><small>${L("召回", "Recall")}</small><b class="mono">${pctx(v.chain_recall)}</b></span><span><small>${L("链路准确率", "Chain precision")}</small><b class="mono">${pctx(v.chain_precision)}</b></span></div>
          <div class="e-dec"><small>${u ? L("规则更新 agent 的决策", "Rule update agent decision") : L("初始训练", "Initial training")}</small>
            ${u ? `<p class="e-why2" title="${H.esc(u.reasoning || "")}">${H.esc(u.reasoning || "")}</p>
              <div class="e-meta">${L(`依据 ${u.inputs?.investigated_cases ?? "—"} 个调查案件`, `${u.inputs?.investigated_cases ?? "—"} investigated cases`)}${u.diff_counts ? ` · <span class="mono">+${u.diff_counts.added} −${u.diff_counts.removed} ~${u.diff_counts.changed}</span>` : ""}</div>`
              : `<p>${prev ? L("决策树重新选叶并合并子树，召回不降、告警减半。", "Leaves re-selected and subtrees merged: recall held, alerts halved.") : L("决策树在开发集标签上拟合，按链路召回选叶；召回优先。", "Decision tree fitted on development labels; leaves chosen for chain recall.")}</p>`}</div></div>`;
      }).join("")}</div>
      ${proposals.length ? `<div class="e-props"><b>${L("下一步 · 规则更新 agent 依据调查证据提出的新特征：", "Next · new features the rule update agent proposed from the evidence:")}</b>${proposals.map(p => `<span title="${H.esc(p.rationale || "")}">${H.esc(p.description || p.template)}</span>`).join("")}</div>` : ""}`;
    H.$$(".e-why, .e-why2").forEach(n => n.addEventListener("click", () => n.classList.toggle("full")));
    const box = H.$("#view-evolution"); box.classList.remove("play"); void box.offsetWidth; box.classList.add("play");
    grow(box);
  }

  /* ---------- live investigation (inline, below the case) ---------- */
  // A live run is remembered per alert on the server: reopening the alert resumes it instead of starting again.
  let liveTimer = null;
  const PHASE = { team_setup: ["高级调查员组队中", "Assembling the team"], investigating: ["调查员调查中", "Investigators working"], aggregating: ["合并判决中", "Merging"],
    senior_investigator: ["高级调查员研判中", "Senior investigator deciding"], decided: ["已结案", "Closed"], stopped: ["已停止", "Stopped"] };

  function liveSection(c, message) {
    const box = H.$("#d-live-sec"); if (!box) return null;
    if (!H.$("#l-net", box)) box.innerHTML = `<h4>⚡ ${L("实时调查", "Live investigation")} <small id="l-status">${message || ""}</small></h4>
      <ol class="l-setup" id="l-setup"></ol>
      <div class="d-row bottom" id="l-panels" hidden><section class="d-sec d-net-sec"><div class="d-tabs" id="l-tabs"></div><div class="d-net"><svg id="l-net"></svg></div></section>
        <section class="d-sec d-replay-sec"><div id="l-replay"><div class="r-feed" id="l-feed"></div></div></section></div>`;
    else if (message) H.$("#l-status", box).textContent = message;
    box.classList.add("on");
    return box;
  }

  async function resumeLive(c) {
    const found = await fetch(`/dashboard/api/live/for?${new URLSearchParams({ account: c.account, day: c.day })}`).then(r => r.ok ? r.json() : null).catch(() => null);
    if (found?.run_id && found.run_id === c.run) return;  // the replay above already shows this investigation
    if (found?.run_id && current === c) pollLive(c, found.run_id);
    else if (!c.investigated && current === c) {
      const box = H.$("#d-live-sec");
      box.innerHTML = `<div class="d-pending"><b>${L("这条告警还没有调查", "This alert has not been investigated yet")}</b><p>${L("点击右上角「立刻调查此告警」：高级调查员分工，调查员分块追查上下游，合并判决后研判，过程会在这里实时显示并保留记录。", "Click “Investigate this alert now”: the team works live here and the record stays with the alert.")}</p></div>`;
    }
  }

  async function startLive(c) {
    const button = H.$("#d-live"); button.disabled = true;
    H.$("#d-live-sec").innerHTML = `<div class="d-pending"><b>${L("正在创建调查…", "Creating the investigation…")}</b></div>`;
    const r = await fetch("/dashboard/api/live/investigate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ alert_id: `alert:${c.account}:${c.day}` }) }).catch(() => null);
    const body = r ? await r.json().catch(() => ({})) : {};
    button.disabled = false;
    if (r && r.ok) {  // reflect the new status at once, in the drawer and in the list row
      c.status = "running";
      const pill = H.$("#d-status"); if (pill) pill.innerHTML = statusPill(c);
      const row = H.$(`.t-row[data-key="${CSS.escape(c.key)}"] .status`); if (row) row.outerHTML = statusPill(c);
      const item = listed.find(x => x.key === c.key); if (item) item.status = "running";
    }
    if (!r || !r.ok) {
      const box = H.$("#d-live-sec");
      // The server explains what is missing (e.g. no NVIDIA API key on this machine) in both languages.
      const detail = body.detail && typeof body.detail === "object" ? L(body.detail.zh, body.detail.en) : String(body.detail || "").replace(/^'|'$/g, "");
      box.innerHTML = `<div class="d-pending error"><b>${L("无法开始实时调查", "Could not start the live investigation")}</b><p>${H.esc(detail || L("实时调查需要完整服务（python -m aml_agent.cli serve）", "Live investigation needs the full API (python -m aml_agent.cli serve)"))}</p></div>`;
      return;
    }
    pollLive(c, body.run_id);
  }

  async function pollLive(c, runId) {
    clearTimeout(liveTimer);
    if (current !== c) return;
    const p = await fetch(`/dashboard/api/live/runs/${runId}`).then(r => r.ok ? r.json() : null).catch(() => null);
    if (!p || current !== c) return;
    // A failed attempt that the server retried: say so once and follow the new run.
    if (p.retried_by) {
      const note = L(`第 ${p.attempt} 次尝试失败：${failure(p.error)}。已自动重试（第 ${p.attempt + 1}/${p.max_attempts} 次）。`,
                     `Attempt ${p.attempt} failed: ${failure(p.error)}. Retrying automatically (${p.attempt + 1}/${p.max_attempts}).`);
      (c._notes = c._notes || []).push(note); toast(note, "warn");
      c._live = null; H.$("#d-live-sec").innerHTML = "";
      return pollLive(c, p.retried_by);
    }
    const m = Math.floor(p.elapsed_seconds / 60), s = p.elapsed_seconds % 60;
    const finished = ["decided", "stopped"].includes(p.phase) && !p.worker_running;
    const failedNow = p.outcome === "failed" && !p.worker_running;
    const retrying = failedNow && p.retry_pending;  // the server starts the retry within ~15 s
    liveSection(c, `${failedNow ? (retrying ? L("本次失败，正在自动重试…", "failed, retrying…") : L("调查失败", "failed")) : tr(PHASE[p.phase]) || p.phase} · `
      + `${finished ? L("调查记录", "record") : L(`已用时 ${m} 分 ${s} 秒`, `${m} min ${s} s`)} · ${L(`第 ${p.attempt}/${p.max_attempts} 次尝试`, `attempt ${p.attempt}/${p.max_attempts}`)} · run ${p.run_id.slice(0, 8)}`
      + (failedNow && !retrying ? L(" · 可点击「立刻调查此告警」重新发起", " · start again with “Investigate this alert now”") : ""));
    const notes = H.$("#l-notes") || H.$("#d-live-sec").insertBefore(H.el("div", "l-notes"), H.$("#l-setup")); notes.id = "l-notes";
    notes.innerHTML = (c._notes || []).map(n => `<div>⟳ ${H.esc(n)}</div>`).join("")
      + (failedNow && !retrying ? `<div class="final">✕ ${H.esc(L(`调查失败：${failure(p.error)}`, `Failed: ${failure(p.error)}`))}</div>` : "");
    if (failedNow && !c._toasted?.[p.run_id]) { (c._toasted = c._toasted || {})[p.run_id] = true; if (!retrying) toast(L(`调查失败：${failure(p.error)}`, `Investigation failed: ${failure(p.error)}`), "bad"); }
    // Preparation before the team exists: each host step with its start time and duration.
    const fmtT = sec => `${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(Math.round(sec % 60)).padStart(2, "0")}`;
    const log = p.setup_log || [], now = p.created + p.elapsed_seconds;
    H.$("#l-setup").innerHTML = [{ at: p.created, zh: "创建调查，发起告警上下文预取", en: "Investigation created; prefetching the alert context" }, ...log].map((x, i, all) => {
      const next = all[i + 1], open = !next && !finished && !p.error && !(p.detail?.members || []).length;
      const took = (next ? next.at : open ? now : x.at) - x.at;
      return `<li class="${open ? "open" : "done"}"><span class="mono">${fmtT(x.at - p.created)}</span><b>${H.esc(L(x.zh, x.en))}</b><small class="mono">${open ? L(`进行中 ${fmtT(took)}`, `running ${fmtT(took)}`) : next ? fmtT(took) : ""}</small></li>`;
    }).join("") + (p.error ? `<li class="failed"><span class="mono">!</span><b>${H.esc(failure(p.error))}</b><small></small></li>` : "");
    if (c.status !== "investigated") {
      c.status = p.outcome === "investigated" ? "investigated" : failedNow && !retrying ? "failed" : "running";
      c.error = c.status === "failed" ? p.error : null;
      const pill = H.$("#d-status"); if (pill) pill.innerHTML = statusPill(c);
    }
    const lc = c._live?.run === runId ? c._live : (c._live = { run: runId, key: "live:" + runId, account: c.account, evaluation: { new_chain_accounts: [] }, _svg: "#l-net", _tabs: "#l-tabs", _filter: null, _count: 0 });
    lc.detail = { hit_rules: [], ...p.detail };
    lc._layout = null;
    H.$("#l-panels").hidden = !lc.detail.members.length;
    if (lc.detail.members.length) { renderTabs(lc); drawNet(lc, Infinity, true); }
    const list = [...(lc.detail.team_plan ? [{ kind: "plan" }] : []), ...lc.detail.steps.map(st => ({ kind: "step", s: st })),
      ...(lc.detail.merge?.conclusion ? [{ kind: "merge" }] : []), ...(lc.detail.senior?.decision ? [{ kind: "senior" }] : [])];
    const feed = H.$("#l-feed");
    for (let i = lc._count; i < list.length; i++) feed.insertAdjacentHTML("beforeend", entry(lc, list[i], !finished && i === list.length - 1));
    if (list.length > lc._count) {
      lc._count = list.length;
      H.$$(".r-entry", feed).forEach((n, i) => n.classList.toggle("cur", i === list.length - 1));
      H.$$(".r-entry:not(.cur) [data-tw]", feed).forEach(x => { clearInterval(x._tw); x.textContent = x.dataset.text; x.removeAttribute("data-tw"); });
      const tw = H.$(".r-entry.cur [data-tw]", feed);
      if (tw) typewrite(tw, tw.dataset.text, () => { feed.scrollTop = feed.scrollHeight; });
      feed.scrollTo({ top: feed.scrollHeight, behavior: "smooth" });
    }
    if (!finished || retrying) liveTimer = setTimeout(() => pollLive(c, runId), 5000);
  }

  function rerender() {
    renderTrace();
    if (view === "evolution") { renderEvolution(); H.renderHistory(); }
    if (current) { const key = current.key; open(key); }
  }

  return {
    init(helpers) {
      H = helpers; DATA = helpers.DATA;
      ready = renderTrace();
      H.$("#scrim").addEventListener("click", close);
      // Page-level "investigate now": open the highest-risk alert on the page that has not been investigated yet.
      H.$("#live-open").addEventListener("click", () => { const next = listed.find(c => !c.investigated) || listed[0]; if (next) open(next.key); });
    },
    show(v) { view = v; if (v === "evolution") { renderEvolution(); H.renderHistory(); } },
    rerender, close,
    // Opening from a link waits for the first page of the list (an alert may only be known from the server).
    open: key => ready.then(() => { if (!listed.some(x => x.key === key)) { filters.q = key.split("|")[0]; page = 0; return renderRows(); } }).then(() => open(key)),
  };
})();
