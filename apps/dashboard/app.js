/* AML·AGENT dashboard: plain JS + SVG, no external libraries.
 * Data comes from /dashboard/data.json (exported from real cycle and run state).
 * Three views: rules (this file), alert tracing and rule evolution (views.js).
 */
(() => {
  "use strict";
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const svgEl = (tag, attrs = {}) => { const e = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; };
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = v => v == null ? "—" : Number(v).toLocaleString("en-US");
  const pct = v => v == null ? "—" : (v * 100).toFixed(1) + "%";
  const fmt = v => v == null ? "—" : Number.isInteger(+v) ? String(+v) : (+v).toFixed(2);
  const params = new URLSearchParams(location.search);
  const tip = $("#tooltip");
  const showTip = (ev, html) => { tip.innerHTML = html; tip.style.left = Math.min(ev.clientX + 14, innerWidth - 380) + "px"; tip.style.top = ev.clientY + 14 + "px"; tip.style.opacity = 1; };
  const hideTip = () => { tip.style.opacity = 0; };
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  let DATA = null, ruleVersion = null;

  const tr = pair => pair ? L(pair[0], pair[1]) : "";

  /* ---------------- theme, language & views ---------------- */
  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("aml-theme", theme);
    $("#theme").textContent = theme === "night" ? "☀" : "☾";
    if (DATA && $("#view-rules").classList.contains("on")) renderTree();
  }
  function setLang(lang) {
    window.LANG = lang; localStorage.setItem("aml-lang", lang);
    $("#lang").textContent = lang === "en" ? "中文" : "EN";
    applyStaticLang();
    if (!DATA) return;
    window.AMLViews.rerender();
    if ($("#view-rules").classList.contains("on")) renderRules();
  }
  function setView(view) {
    $$(".tab").forEach(t => t.classList.toggle("on", t.dataset.view === view));
    $$(".view").forEach(v => v.classList.toggle("on", v.id === "view-" + view));
    if (view === "rules") renderRules();
    window.AMLViews?.show(view);
    history.replaceState(null, "", `?view=${view}`);
    scrollTo({ top: 0 });
  }

  /* ---------------- modal: feature computation ---------------- */
  function featureModal(name) {
    const f = DATA.features[name]; if (!f) return;
    const uses = [];
    for (const v of [selectedVersion(), DATA.versions.find(x => x.id === DATA.active_version)].filter((x, i, all) => x && all.indexOf(x) === i)) {
      v.rules.groups.forEach((g, i) => { for (const a of g.atoms) if (a.feature === name) uses.push(`${v.label} · ${L("规则", "Rule")} ${i + 1}: ${a.text}`); });
    }
    const d = f.distribution;
    $("#modal-card").classList.remove("wide");
    $("#modal-card").innerHTML = `
      <button class="btn-ghost close">${L("关闭", "Close")}</button>
      <h3>${esc(L(f.zh, name))}</h3><code>${esc(name)}</code>
      <div class="kv">
        <span>${L("计算方式", "Computation")}</span><div>${esc(L(f.calc, f.description || f.calc))}</div>
        <span>${L("计算公式", "Formula")}</span><pre>${esc(f.formula)}</pre>
        <span>${L("时间窗口", "Window")}</span><div>${esc(DATA.dataset.window)}</div>
        <span>${L("Zetaris 表", "Zetaris table")}</span><div><code>${esc(f.table)}</code></div>
        <span>${L("单位", "Unit")}</span><div>${esc(f.unit)}</div>
        <span>${L("AUSTRAC 原文", "AUSTRAC text")}</span><div>${f.policy.map(p => DATA.policies[p]).filter(Boolean).map(q => `“${esc(q.text)}”`).join("<br>")}</div>
        <span>${L("开发期分布", "Development distribution")}</span><div class="dist">${d ? ["p50", "p90", "p99"].map(k => `<div><b>${fmt(d[k])}</b>${k}</div>`).join("") : "—"}</div>
        <span>${L("被规则使用", "Used by")}</span><div>${uses.length ? uses.map(esc).join("<br>") : L("未被当前规则使用", "Not used by the current rules")}</div>
      </div>`;
    $("#modal").classList.add("on");
  }

  /* ---------------- rules view ---------------- */
  const POLICY_SHORT = { P02: ["多方汇入", "Third-party fan-in"], P03: ["快速过境", "Rapid pass-through"], P04: ["画像不符", "Profile mismatch"] };
  const policyTag = p => POLICY_SHORT[p] ? L(...POLICY_SHORT[p]) : L(DATA.policies[p]?.zh || p, DATA.policies[p]?.section || p);
  const fname = f => L(DATA.features[f]?.zh || f, f);
  const condText = a => `${esc(fname(a.feature))} ${a.operator === "ge" ? "≥" : "≤"} ${fmt(a.threshold)}`;
  let focusGroup = null, ruleSort = "windows", showAll = false, showEarly = false;

  const selectedVersion = () => DATA.versions.find(v => v.id === ruleVersion) || DATA.versions.find(v => v.id === DATA.active_version) || DATA.versions[DATA.versions.length - 1];
  const STATUS = { published: ["已发布", "Published"], held: ["暂缓", "Held"], reference: ["参考", "Reference"] };
  const TRIGGER = {
    initial_training: ["初始训练：决策树在开发集标签上拟合", "Initial training: decision tree fitted on development labels"],
    rule_update_agent: ["规则更新 agent：依据调查结论与误报对比制定方案", "Rule update agent: plan from investigation outcomes and false-alarm contrast"],
    rule_update_fallback: ["规则更新（agent 方案无效，沿用上一版配置）", "Rule update (agent plan invalid; previous settings reused)"],
  };
  const OBJECTIVE = { chain_recall: ["高召回（链路召回 ≥ 97%）", "High recall (chain recall ≥ 97%)"], daily_budget: ["旧目标（每天 200 个告警）", "Earlier objective (200 alerts a day)"], reference: ["人工设定阈值", "Hand-set thresholds"] };
  const chainOf = label => (DATA.showcase?.rule_versions || []).find(x => x.label === label);
  function delta(now, before, kind) {
    if (now == null || before == null) return "";
    const d = now - before, good = kind === "fewer" ? d < 0 : d > 0;
    if (Math.abs(d) < 1e-9) return `<span class="delta">${L("持平", "flat")}</span>`;
    const text = kind === "fewer" ? `${d > 0 ? "+" : ""}${num(Math.round(d))}` : `${d > 0 ? "+" : ""}${(d * 100).toFixed(1)} pp`;
    return `<span class="delta ${good ? "plus-c" : "minus-c"}">${L("较", "vs")} ${esc(selectedVersion().parent_label || "")} ${text}</span>`;
  }

  function renderVersions() {
    const box = $("#versions"); box.innerHTML = "";
    const early = DATA.versions.filter(v => v.objective !== "chain_recall");
    const main = DATA.versions.filter(v => v.objective === "chain_recall" && v.status === "published");
    const card = (v, i) => {
      const m = chainOf(v.label) || v.validation || {}, active = v.id === DATA.active_version, agent = v.trigger === "rule_update_agent";
      const node = el("div", "vcard" + (v.id === selectedVersion().id ? " on" : "") + (agent ? " agent" : ""),
        `<div class="vtop"><b>${esc(v.label)}</b><span class="st ${active ? "active" : v.status}">${active ? L("当前", "Live") : tr(STATUS[v.status])}</span></div>
         <small class="mono">${v.objective === "chain_recall" ? `${L("召回", "recall")} ${pct(m.chain_recall)} · ${num(m.flagged_per_day)}${L("/天", "/day")}` : v.objective === "daily_budget" ? `TP ${num(m.tp)} · p ${pct(m.precision)}` : L("人工阈值", "hand-set")}</small>
         <small class="src">${agent ? L("规则更新 agent", "Rule update agent") : v.trigger === "initial_training" ? L("初始训练", "Initial training") : v.trigger ? L("规则更新·兜底", "Fallback update") : L("人工", "Manual")}${v.parent_label ? ` · ${L("父", "parent")} ${esc(v.parent_label)}` : ""}</small>`);
      node.addEventListener("click", () => { ruleVersion = v.id; focusGroup = null; renderHistory(); });
      return node;
    };
    const fold = el("div", "vcard early" + (showEarly ? " on" : ""), `<b class="mono">${esc(early[0]?.label || "")} – ${esc(early[early.length - 1]?.label.split(" ")[0] || "")}</b><small>${L("早期目标 · 每天 200 告警", "Earlier objective · 200 alerts a day")}</small><small class="src">${showEarly ? L("点击收起", "click to fold") : L("点击展开", "click to expand")}</small>`);
    fold.addEventListener("click", () => { showEarly = !showEarly; renderVersions(); });
    box.appendChild(fold);
    if (showEarly) early.forEach(v => { box.appendChild(el("div", "vlink")); box.appendChild(card(v)); });
    main.forEach(v => { box.appendChild(el("div", "vlink" + (v.trigger === "rule_update_agent" ? " agent" : ""))); box.appendChild(card(v)); });
  }

  function renderVersionDetail(v) {
    const plan = v.plan || {}, fit = v.fit || {};
    const when = v.created ? new Date(v.created * 1000).toLocaleString(LANG === "en" ? "en-AU" : "zh-CN") : "—";
    const rows = [
      [L("版本", "Version"), `${esc(v.label)} · ${tr(STATUS[v.status])}${v.id === DATA.active_version ? L(" · 当前生效", " · live") : ""}`],
      [L("来源", "Source"), esc(tr(TRIGGER[v.trigger]) || L("人工设定的参考规则", "Hand-set reference rules"))],
      [L("目标", "Objective"), esc(tr(OBJECTIVE[v.objective]) || "—")],
      [L("父版本 · 生成时间", "Parent · created"), `${esc(v.parent_label || L("无", "none"))} · ${esc(when)}`],
      ...(v.trigger ? [
        [L("训练配置", "Training setup"), L(`${(plan.features || []).length} 个指标 · ${plan.max_leaf_nodes ? `最多 ${plan.max_leaf_nodes} 个叶子` : `树深 ${plan.max_depth ?? "—"}`} · 最小叶子 ${plan.min_samples_leaf ?? "—"}`, `${(plan.features || []).length} features · ${plan.max_leaf_nodes ? `up to ${plan.max_leaf_nodes} leaves` : `depth ${plan.max_depth ?? "—"}`} · min leaf ${plan.min_samples_leaf ?? "—"}`)],
        [L("训练数据", "Training data"), L(`${num(v.development_sample?.rows)} 行开发集样本${v.investigated_cases ? ` · 含 ${v.investigated_cases} 个调查案件反馈` : ""}`, `${num(v.development_sample?.rows)} development rows${v.investigated_cases ? ` · ${v.investigated_cases} investigated cases` : ""}`)],
        [L("开发集拟合", "Development fit"), fit.development_chain_recall != null ? L(`链路召回 ${pct(fit.development_chain_recall)} · ${fit.leaves} 个叶子选 ${fit.chosen_leaves} 个`, `chain recall ${pct(fit.development_chain_recall)} · ${fit.chosen_leaves} of ${fit.leaves} leaves`) : "—"],
        [L("门禁结论", "Gate"), `<b>${v.decision === "accept_for_experiment" ? L("接受", "Accepted") : v.decision ? L("暂缓", "Held") : "—"}</b> ${esc(v.reason || "")}`],
      ] : []),
    ];
    $("#version-detail").innerHTML = rows.map(([k, val]) => `<div class="kv2"><span>${k}</span><div>${val}</div></div>`).join("")
      + (plan.rationale && v.trigger !== "initial_training" ? `<div class="agent-reason"><span>${L("规则更新 agent 的理由", "Rule update agent's reasoning")}</span><p>${esc(plan.rationale)}</p></div>` : "");
  }

  function renderDiff(v) {
    const box = $("#rule-diff"), d = v.diff;
    if (!d) { box.innerHTML = `<div class="note">${v.objective === "reference" ? L("参考规则没有父版本。", "Reference rules have no parent.") : L("该版本生成于版本对比功能上线之前，没有记录规则变化。", "This version predates diff tracking.")}</div>`; return; }
    const conds = list => list.map(esc).join(L(" 且 ", " AND "));
    const chip = (label, n, cls) => `<span class="dchip"><small>${label}</small><b class="${cls}">${n}</b></span>`;
    box.innerHTML = `<div class="dsum"><b>${L(`相对 ${esc(v.parent_label || "父版本")} 的规则变化`, `Changes against ${esc(v.parent_label || "parent")}`)}</b>
        ${chip(L("新增", "added"), d.added.length, "plus-c")}${chip(L("删除", "removed"), d.removed.length, "minus-c")}${chip(L("调整", "changed"), d.changed.length, "")}${chip(L("不变", "same"), d.unchanged, "dim")}</div>
      ${d.added.slice(0, 3).map(g => `<div class="ditem add">＋ ${conds(g.conditions)}</div>`).join("")}
      ${d.removed.slice(0, 3).map(g => `<div class="ditem del">－ ${conds(g.conditions)}</div>`).join("")}
      ${d.changed.slice(0, 4).map(c => `<div class="ditem chg">${L("调整", "changed")}  ${[...c.thresholds.map(t => `${esc(t.condition)} ${fmt(t.from)} → <b>${fmt(t.to)}</b>`),
          ...c.added_conditions.map(x => `＋ ${esc(x)}`), ...c.removed_conditions.map(x => `－ ${esc(x)}`)].join("；")}</div>`).join("")}`;
  }

  function renderRules() {
    const v = selectedVersion(), r = v.rules, m = chainOf(v.label) || v.validation || {}, p = chainOf(v.parent_label) || v.parent_validation || {};
    const tag = $("#current-version");
    tag.innerHTML = `<i></i>${esc(v.label)} · ${v.id === DATA.active_version ? L("当前生效", "live") : tr(STATUS[v.status])}`;
    tag.className = "vtag" + (v.id === DATA.active_version ? "" : " past");
    const cited = [...new Set(r.groups.flatMap(g => g.policy))];
    const kpi = (label, value, note) => `<div class="r-kpi"><span>${label}</span><b class="mono">${value}</b><small>${note}</small></div>`;
    $("#rule-summary").innerHTML = kpi(L("规则", "Rules"), L(`${r.groups.length} 条`, `${r.groups.length}`), L(`${r.atom_count} 个指标条件 · 引用 ${cited.length} 条 AUSTRAC 指标`, `${r.atom_count} conditions · ${cited.length} AUSTRAC indicators`))
      + (v.objective === "chain_recall"
        ? kpi(L("验证期链路召回", "Validation chain recall"), pct(m.chain_recall), `${delta(m.chain_recall, p.chain_recall)} · ${L("目标 ≥ 97%", "target ≥ 97%")}`)
          + kpi(L("每天告警", "Alerts a day"), num(m.flagged_per_day), delta(m.flagged_per_day, p.flagged_per_day, "fewer"))
          + kpi(L("链路准确率", "Chain precision"), pct(m.chain_precision), delta(m.chain_precision, p.chain_precision))
        : kpi(L("版本目标", "Objective"), esc(tr(OBJECTIVE[v.objective]) || "—"), "") + kpi(L("验证期 precision", "Validation precision"), v.objective === "daily_budget" ? pct(m.precision) : "—", "") + kpi(L("验证期 TP", "Validation TP"), v.objective === "daily_budget" ? num(m.tp) : "—", ""));

    const numbered = r.groups.map((g, i) => ({ g, no: i + 1 }));
    const sorted = numbered.slice().sort((a, b) => ruleSort === "precision" ? (b.g.estimated_precision ?? -1) - (a.g.estimated_precision ?? -1) : (b.g.estimated_windows ?? -1) - (a.g.estimated_windows ?? -1));
    const shown = showAll ? sorted : sorted.slice(0, 6);
    const list = $("#rule-list"); list.innerHTML = "";
    shown.forEach(({ g, no }, k) => {
      if (k) list.appendChild(el("div", "rule-or", `<span>${L("或", "OR")}</span>`));
      const open = focusGroup === g.id;
      const card = el("div", "rule" + (open ? " open" : ""));
      card.innerHTML = `
        <div class="rhead"><span class="rno">${L("规则", "Rule")} ${no}</span><code>${esc(g.id)}</code>
          ${g.policy.length ? `<span class="ptag"><small>AUSTRAC</small>${[...new Set(g.policy)].map(pp => esc(policyTag(pp))).join(" · ")}</span>` : ""}
          <span class="sp"></span>
          <span class="rstat mono">${g.estimated_precision != null ? L(`≈${num(g.estimated_windows)} 账户日 · 准确率 ${pct(g.estimated_precision)}`, `≈${num(g.estimated_windows)} account-days · precision ${pct(g.estimated_precision)}`) : v.objective === "reference" ? L("人工设定阈值", "hand-set") : L(`${esc(v.label)} 新增 · 尚无全量统计`, `new in ${esc(v.label)} · no population stats yet`)}</span>
          <span class="chev">${open ? "▴" : "▾"}</span></div>
        <div class="rconds">${g.atoms.map(a => `<span>${condText(a)}</span>`).join(`<i class="and">${L("且", "AND")}</i>`)}</div>
        <div class="rbody">${g.atoms.map(a => { const f = DATA.features[a.feature] || {}, d = f.distribution;
          return `<div class="ind"><b>${esc(fname(a.feature))}</b><p>${esc(L(f.calc, f.description || f.calc) || "")}${d ? `<small class="mono">p50 ${fmt(d.p50)} · p90 ${fmt(d.p90)} · p99 ${fmt(d.p99)}</small>` : ""}</p><button class="btn-ghost sm" data-feature="${esc(a.feature)}">${L("公式", "Formula")}</button></div>`; }).join("")}
          ${quotes(g.policy).map(q => `<blockquote><span>AUSTRAC ${L("原文", "text")} · ${esc(L(q.zh, q.section))}</span>“${esc(q.text)}”</blockquote>`).join("")}
          ${g.explanation || g.concerns ? `<div class="rnote">${g.explanation ? esc(g.explanation) : ""}${g.concerns ? `<br><em>⚠ ${esc(g.concerns)}</em>` : ""}</div>` : ""}</div>`;
      $$("[data-feature]", card).forEach(b => b.addEventListener("click", ev => { ev.stopPropagation(); featureModal(b.dataset.feature); }));
      $(".rhead", card).addEventListener("click", () => { focusGroup = open ? null : g.id; renderRules(); });
      list.appendChild(card);
    });
    if (sorted.length > 6) {
      const more = el("button", "rule-more", showAll ? L("收起", "Show fewer") : L(`显示其余 ${sorted.length - 6} 条规则`, `Show the other ${sorted.length - 6} rules`));
      more.addEventListener("click", () => { showAll = !showAll; renderRules(); });
      list.appendChild(more);
    }
    renderTree();
  }
  // The version record lives on the rule evolution page; selecting a version also switches the rules page.
  function renderHistory() { const v = selectedVersion(); renderVersions(); renderVersionDetail(v); renderDiff(v); }
  const quotes = refs => [...new Set(refs)].map(p => DATA.policies[p]).filter(Boolean);

  function renderTree(target, full = false) {
    const svg = target || $("#tree"); svg.innerHTML = "";
    const v = selectedVersion(), r = v.rules, nodes = r?.tree;
    const number = Object.fromEntries((r?.groups || []).map((g, i) => [g.id, i + 1]));
    if (!nodes || !nodes.length) {
      svg.setAttribute("viewBox", "0 0 420 60"); svg.style.width = "420px"; svg.style.height = "60px";
      const t = svgEl("text", { x: 10, y: 34, fill: css("--dim") }); t.textContent = v.status === "reference" ? L("人工参考规则是手工阈值，没有决策树。", "Reference rules are hand-set; no tree.") : L("该版本没有导出决策树。", "No tree was exported for this version."); svg.appendChild(t);
      if (!target) $("#tree-note").textContent = "";
      return;
    }
    // Only the paths that lead to a rule: unselected leaves and everything below a rule's root are hidden.
    const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
    const parent = {}; for (const n of nodes) if (n.left != null) { parent[n.left] = n.id; parent[n.right] = n.id; }
    const keep = new Set();
    for (const n of nodes) if (n.group_id) for (let id = n.id; id != null; id = parent[id]) keep.add(id);
    const children = id => { const n = byId[id]; return n.group_id || n.left == null ? [] : [n.left, n.right].filter(c => keep.has(c)); };
    const focusRoot = focusGroup ? nodes.find(n => n.group_id === focusGroup)?.id : null;
    const onPath = new Set(); for (let id = focusRoot; id != null; id = parent[id]) onPath.add(id);
    // Full screen: cards with wide gaps for the 是/否 labels. Side column: compact dots sized to fit its width.
    const depthOf = id => { let d = 0; for (let x = id; parent[x] != null; x = parent[x]) d++; return d; };
    const maxDepth = Math.max(...[...keep].map(depthOf));
    const room = (svg.parentElement.clientWidth || 600) - 28;
    const compact = !full;
    const BW = compact ? 0 : 120, BH = compact ? 0 : 58, GY = compact ? 40 : 74;
    const GX = compact ? Math.max(26, (room - 110) / Math.max(1, maxDepth)) : 190;
    let slot = 0; const pos = {};
    const place = (id, depth) => {
      const kids = children(id);
      if (!kids.length) { pos[id] = { x: depth * GX, y: slot++ * GY }; return; }
      kids.forEach(c => place(c, depth + 1));
      pos[id] = { x: depth * GX, y: kids.reduce((s, c) => s + pos[c].y, 0) / kids.length };
    };
    place(0, 0);
    const w = Math.max(...Object.values(pos).map(p => p.x)) + BW + 10, h = Math.max(...Object.values(pos).map(p => p.y)) + BH + 10;
    const scale = full ? Math.min(1.25, room / w) : 1;
    const extra = compact ? 100 : 0;  // room for the last label
    svg.setAttribute("viewBox", `-10 -16 ${w + extra} ${h + 26}`); svg.style.width = (w + extra) * scale + "px"; svg.style.height = (h + 26) * scale + "px";
    for (const id of Object.keys(pos).map(Number)) for (const child of children(id)) {
      const n = byId[id], label = child === n.left ? L("是", "yes") : L("否", "no");
      const a = pos[id], b = pos[child], x1 = a.x + BW, y1 = a.y + BH / 2, x2 = b.x, y2 = b.y + BH / 2;
      const cls = "tlink" + (onPath.has(child) ? " focus" : " kept");
      svg.appendChild(svgEl("path", { d: `M${x1},${y1} C${x1 + 40},${y1} ${x2 - 40},${y2} ${x2},${y2}`, class: cls }));
      const t = svgEl("text", { x: x1 + (x2 - x1) * (compact ? 0.5 : 0.45), y: y1 + (y2 - y1) * (compact ? 0.5 : 0.45) - 4, class: "tlab", "text-anchor": "middle" });
      t.textContent = label; svg.appendChild(t);
    }
    for (const id of Object.keys(pos).map(Number)) {
      const n = byId[id], p = pos[id], gid = n.group_id;
      const cls = "tnode" + (gid ? " kept" : " split") + (id === focusRoot ? " focus" : onPath.has(id) ? " onpath" : "");
      const g = svgEl("g", { class: cls, transform: `translate(${p.x},${p.y})` });
      if (compact) {
        g.appendChild(svgEl("rect", { x: gid ? -7 : -5, y: gid ? -7 : -5, width: gid ? 14 : 10, height: gid ? 14 : 10, rx: gid ? 4 : 5 }));
        // Split conditions show on hover here; the full-screen tree prints them on every card.
        if (gid) { const t = svgEl("text", { x: 12, y: 4, class: "strong" }); t.textContent = `${L("规则", "Rule")} ${number[gid]}`; g.appendChild(t); }
      } else g.appendChild(svgEl("rect", { width: BW, height: BH, rx: 8 }));
      const lines = compact ? [] : gid
        ? [`${L("规则", "Rule")} ${number[gid]}`, `p ${pct(n.estimated_population_precision)}`, `≈${num(n.estimated_population_windows)}`]
        : [fname(n.feature), `≤ ${fmt(n.threshold)}`, `p ${pct(n.estimated_population_precision)}`];
      lines.forEach((text, k) => {
        const t = svgEl("text", { x: 8, y: 18 + k * 16, class: k === 0 ? (gid ? "strong" : "") : "dim" });
        t.textContent = text.length > 8 && k === 0 && !gid ? text.slice(0, 8) + "…" : text; g.appendChild(t);
      });
      g.addEventListener("mousemove", ev => showTip(ev, `${gid ? `<b>${L("规则", "Rule")} ${number[gid]}</b><br>${L("该节点以下全部告警；点击展开右侧规则", "Everything below alerts; click to open the rule")}` : `<b>${esc(fname(n.feature))} ≤ ${fmt(n.threshold)}</b><br>${L("是 → 上分支，否 → 下分支；点击查看指标公式", "yes → upper branch, no → lower; click for the formula")}`}<br>${L("训练样本：正例", "Training: positives")} ${num(n.sample_positives)} · ${L("负例", "negatives")} ${num(n.sample_negatives)}<br>${L("按开发期全量人群加权", "Population-weighted")}: ≈${num(n.estimated_population_windows)} ${L("个账户日", "account-days")}, precision ${pct(n.estimated_population_precision)}`));
      g.addEventListener("mouseleave", hideTip);
      g.addEventListener("click", ev => {
        ev.stopPropagation();
        if (gid) { focusGroup = focusGroup === gid ? null : gid; hideTip(); $("#modal").classList.remove("on"); renderRules(); }
        else featureModal(n.feature);
      });
      svg.appendChild(g);
    }
    if (!target) {
      if (focusRoot != null) requestAnimationFrame(() => { const wrap = svg.parentElement; wrap.scrollTop = pos[focusRoot].y * scale - wrap.clientHeight / 2; });
      const hidden = nodes.filter(n => n.left == null).length - nodes.filter(n => n.group_id).length;
      $("#tree-note").textContent = L(`训练：${nodes.filter(n => n.left == null).length} 个叶子中选 ${nodes.filter(n => n.group_id).length} 个作为规则；只显示通往规则的路径，悬停看分裂条件，点击全屏。`, `Training: ${nodes.filter(n => n.group_id).length} of ${nodes.filter(n => n.left == null).length} leaves became rules; only paths to rules are shown. Hover for splits, click for full screen.`);
    }
  }

  function treeFullscreen() {
    const card = $("#modal-card");
    card.classList.add("wide");
    card.innerHTML = `<button class="btn-ghost close">${L("关闭", "Close")}</button><h3>${L("决策树", "Decision tree")} · ${esc(selectedVersion().label)}</h3><div class="tree-full"><svg id="tree-big"></svg></div>`;
    $("#modal").classList.add("on");
    requestAnimationFrame(() => renderTree($("#tree-big"), true));
  }

  /* ---------------- boot ---------------- */
  async function boot() {
    setTheme(params.get("theme") || localStorage.getItem("aml-theme") || "day");
    $("#theme").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "night" ? "day" : "night"));
    $("#lang").addEventListener("click", () => setLang(LANG === "en" ? "zh" : "en"));
    $$(".tab").forEach(t => t.addEventListener("click", () => setView(t.dataset.view)));
    $("#toggle-history").addEventListener("click", () => { setView("evolution"); setTimeout(() => $("#history").scrollIntoView({ behavior: "smooth" }), 100); });
    $$("#rule-sort button").forEach(b => b.addEventListener("click", () => { ruleSort = b.dataset.sort; $$("#rule-sort button").forEach(x => x.classList.toggle("on", x === b)); renderRules(); }));
    $("#modal").addEventListener("click", e => { if (e.target.id === "modal" || e.target.classList.contains("close")) { $("#modal").classList.remove("on"); $("#modal-card").classList.remove("wide"); } });
    $(".tree-block .icon-btn").addEventListener("click", treeFullscreen);
    $("#tree").parentElement.addEventListener("click", treeFullscreen);
    addEventListener("keydown", e => { if (e.key === "Escape") { $("#modal").classList.remove("on"); window.AMLViews?.close(); } });
    setLang(LANG);
    DATA = await fetch("/dashboard/data.json").then(r => r.json());
    $("#cycle-id").textContent = new Date(DATA.generated_at * 1000).toLocaleString("zh-CN");
    window.AMLViews.init({ DATA, $, $$, el, esc, num, fmt, svgEl, showTip, hideTip, featureModal, renderHistory });
    focusGroup = params.get("rule");
    setView(params.get("view") || "trace");
    if (params.get("case")) window.AMLViews.open(params.get("case"));
  }
  boot();
})();
