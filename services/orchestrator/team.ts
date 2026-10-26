/** Team investigation: coordinator split, parallel specialists, World Model case graph, aggregator.
 *
 * The business API owns case state and decisions; the official engines own the
 * loops (Markovian), per-investigator memory (H-MEM) and the shared case graph
 * (World Model). Specialists never see each other's context; they meet only in
 * the World Model, which the aggregator reads.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { WorldModel } from '../../vendor/meterless/world-model/src/index.ts';
import { runCase } from './worker.ts';
import { apiClient } from './http.ts';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const base = 'http://127.0.0.1:8010';

interface Row { transaction_id: string | number; transaction_time: string; from_bank: string; from_account: string;
  to_bank: string; to_account: string; amount_paid: string; payment_currency: string }
interface ChildState { id: string; status: string; request: { account: { bank: string; account: string }; split: string;
  assignment: { role: string; question: string; accounts: string[] } };
  evidence: Record<string, { rows?: Row[]; transaction_context?: Record<string, Record<string, unknown>>;
    account_context?: Record<string, Record<string, Record<string, number | null>>> }>; observations: { evidence_ref: string; digest?: string[]; arguments?: Record<string, unknown> }[];
  conclusion?: { conclusion?: string; supported_findings?: string[]; counterevidence?: string[]; suspect_transaction_ids?: string[] } }

const accountId = (ref: string) => `account:acct:${ref}`;
const transactionId = (id: string) => `transaction:tx:${id}`;

export async function investigateTeam(parentId: string): Promise<void> {
  const token = process.env.AML_INTERNAL_TOKEN || fs.readFileSync(path.join(root, '.runtime/api-token'), 'utf8').trim();
  // Coordinator and aggregator calls go to Ultra, which can queue for minutes under the shared rate limit.
  const request = apiClient(base, token, 2_400_000);

  const team = await request<{ children: string[]; assignments: { role: string }[]; plan_source: string }>(`/internal/runs/${parentId}/team`, {});
  console.log(JSON.stringify({ team: parentId, plan_source: team.plan_source, roles: team.assignments.map(a => a.role) }));
  // Specialists run concurrently; one failing never stops the others.
  const outcomes = await Promise.allSettled(team.children.map(child => runCase(child)));
  outcomes.forEach((outcome, index) => console.log(JSON.stringify({ specialist: team.children[index],
    ok: outcome.status === 'fulfilled', error: outcome.status === 'rejected' ? String(outcome.reason).slice(0, 160) : undefined })));

  const children = await Promise.all(team.children.map(id => request<ChildState>(`/v1/runs/${id}`)));
  const split = children[0]?.request.split ?? 'unknown';
  const world = new WorldModel({ namespace: `case-${parentId}`, storage: 'local', directory: path.join(root, '.runtime/world', split) });
  const reportedBy = new Map<string, Set<string>>();
  const suspectedBy = new Map<string, Set<string>>();
  const at = new Date();

  // Every specialist (including a follow-up one) writes into the same case World Model.
  const ingest = async (child: ChildState) => {
    const role = child.request.assignment.role;
    const source = { kind: 'agent' as const, by: `specialist:${role}`, at, runId: child.id };
    for (const id of child.conclusion?.suspect_transaction_ids ?? []) {
      if (!suspectedBy.has(id)) suspectedBy.set(id, new Set());
      suspectedBy.get(id)!.add(role);
    }
    for (const evidence of Object.values(child.evidence)) {
      // Zetaris-computed endpoint profiles become account attributes in the shared graph.
      for (const [ref, days] of Object.entries(evidence.account_context ?? {})) {
        const latest = Object.keys(days).sort().pop();
        if (latest) await world.upsertEntity({ type: 'account', externalKey: { system: 'acct', id: ref }, name: ref, source,
          attrs: { profile_day: latest, ...days[latest] } });
      }
      for (const row of evidence.rows ?? []) {
        const id = String(row.transaction_id);
        const payer = `${row.from_bank}:${row.from_account}`;
        const payee = `${row.to_bank}:${row.to_account}`;
        if (!reportedBy.has(id)) reportedBy.set(id, new Set());
        reportedBy.get(id)!.add(role);
        // Stable external IDs: every specialist resolves the same account and transaction.
        await world.upsertEntity({ type: 'account', externalKey: { system: 'acct', id: payer }, name: payer, source });
        await world.upsertEntity({ type: 'account', externalKey: { system: 'acct', id: payee }, name: payee, source });
        await world.upsertEntity({ type: 'transaction', externalKey: { system: 'tx', id }, source,
          attrs: { time: row.transaction_time, amount: row.amount_paid, currency: row.payment_currency,
            ...(evidence.transaction_context?.[id] ?? {}) } });
        await world.relate({ from: accountId(payer), type: 'paid', to: transactionId(id), validFrom: row.transaction_time, source });
        await world.relate({ from: transactionId(id), type: 'credited', to: accountId(payee), validFrom: row.transaction_time, source });
      }
    }
    const conclusion = child.conclusion ?? {};
    for (const ref of child.request.assignment.accounts) {
      await world.upsertEntity({ type: 'account', externalKey: { system: 'acct', id: ref }, name: ref, source });
      await world.assertFact({ about: accountId(ref), predicate: `finding:${role}`, source,
        value: { status: child.status, conclusion: conclusion.conclusion ?? 'none', findings: conclusion.supported_findings ?? [],
          counter: conclusion.counterevidence ?? [], suspects: conclusion.suspect_transaction_ids ?? [] } });
    }
  };
  for (const child of children) await ingest(child);

  const focus = `${children[0]?.request.account.bank}:${children[0]?.request.account.account}`;
  type Final = { status: string; conclusion?: { suspect_transaction_ids?: string[] } };
  type Verdict = { decision?: string; risk_level?: string; status?: string; follow_up_child?: string | null };
  const decide = async (): Promise<[Final, Verdict]> => {
    const context = buildContext(world, focus, reportedBy, suspectedBy);
    fs.writeFileSync(path.join(root, '.runtime/world', split, `case-${parentId}.context.txt`), context);
    const final = await request<Final>(`/internal/runs/${parentId}/aggregate`, { world_context: context });
    // The team investigates; the senior investigator decides the risk level and whether to report.
    let verdict: Verdict = {};
    if (final.status === 'completed' || final.status === 'insufficient_evidence') {
      try {
        verdict = await request<Verdict>(`/internal/runs/${parentId}/adjudicate`, {});
      } catch (error) {
        console.log(JSON.stringify({ adjudication_error: String(error).slice(0, 200) }));
      }
    }
    return [final, verdict];
  };
  let [final, adjudication] = await decide();
  // The senior investigator may dispatch one follow-up specialist, then re-decides with the merged evidence.
  if (adjudication.follow_up_child) {
    const extra = adjudication.follow_up_child;
    console.log(JSON.stringify({ follow_up: extra }));
    try { await runCase(extra); } catch (error) { console.log(JSON.stringify({ follow_up_error: String(error).slice(0, 160) })); }
    await ingest(await request<ChildState>(`/v1/runs/${extra}`));
    [final, adjudication] = await decide();
  }
  const confirmed = new Set(final.conclusion?.suspect_transaction_ids ?? []);
  fs.writeFileSync(path.join(root, '.runtime/world', split, `case-${parentId}.mmd`), mermaid(world, focus, suspectedBy, confirmed));
  console.log(JSON.stringify({ team: parentId, status: final.status, decision: adjudication.decision ?? null,
    risk_level: adjudication.risk_level ?? null, world_events: world.events().length }));
}

/** Directed, time-ordered views over the merged World Model, within a line budget. */
export function buildContext(world: WorldModel, focus: string, reportedBy: Map<string, Set<string>>,
  suspectedBy: Map<string, Set<string>> = new Map()): string {
  const graph = world.view('graph');
  const facts = world.view('facts');
  const tx = (id: string) => graph.entity(id);
  const payee = (txId: string) => graph.neighborsOf(txId, { via: 'credited' }).find(e => e.type === 'account');
  const payer = (txId: string) => graph.incoming(txId, { via: 'paid' })[0];
  const sent = (acct: string) => graph.neighborsOf(acct, { via: 'paid' }).filter(e => e.type === 'transaction');
  const received = (acct: string) => graph.incoming(acct, { via: 'credited' }).filter(e => e.type === 'transaction');
  const time = (id: string) => String(tx(id)?.attrs.time ?? '');
  const ref = (id?: string) => id?.replace('account:acct:', '') ?? '?';
  const label = (id: string) => `${id.replace('transaction:tx:', '')}(${time(id).slice(5, 16).replace('T', ' ')} ${tx(id)?.attrs.amount} ${tx(id)?.attrs.currency})`;
  const flags = (id: string) => {
    const a = tx(id)?.attrs ?? {};
    const parts: string[] = [];
    if (a.payer_prior_in_gap_h != null) parts.push(`payer received ${a.payer_prior_in_gap_h}h before`);
    if (a.payer_prior_other_currency) parts.push('payer received another currency');
    if (a.reverse_edge_prior_48h) parts.push('reverse of an earlier payment');
    return parts.length ? ` [${parts.join('; ')}]` : '';
  };
  const profile = (ref: string) => {
    const a = graph.entity(accountId(ref))?.attrs ?? {};
    return a.profile_day ? `sources48h=${a.fan_in_48h} payees24h=${a.fan_out_24h} currencies=${a.currency_count_24h} receive-then-send=${a.transit_gap_hours ?? 'none'}h activity x${a.activity_change_24h}` : 'no profile';
  };

  const chains: string[] = [];
  function forward(acct: string, after: string, trail: string[], depth: number) {
    const next = sent(acct).filter(t => time(t.id) > after).sort((a, b) => time(a.id).localeCompare(time(b.id))).slice(0, 3);
    if (!next.length || depth === 4) { if (trail.length > 1) chains.push('DOWN ' + trail.join(' > ')); return; }
    for (const t of next) forward(payee(t.id)?.id ?? '', time(t.id), [...trail, label(t.id), ref(payee(t.id)?.id)], depth + 1);
  }
  function backward(acct: string, before: string, trail: string[], depth: number) {
    const prior = received(acct).filter(t => time(t.id) < before).sort((a, b) => time(b.id).localeCompare(time(a.id))).slice(0, 3);
    if (!prior.length || depth === 4) { if (trail.length > 1) chains.push('UP ' + trail.join(' < ')); return; }
    for (const t of prior) backward(payer(t.id)?.id ?? '', time(t.id), [...trail, label(t.id), ref(payer(t.id)?.id)], depth + 1);
  }
  forward(accountId(focus), '', [focus], 0);
  backward(accountId(focus), '9999', [focus], 0);

  const accounts = graph.query({ type: 'account' });
  const transactions = graph.query({ type: 'transaction' }).sort((a, b) => time(a.id).localeCompare(time(b.id)));
  const lines = [
    `case graph: ${accounts.length} accounts, ${transactions.length} transactions, focus ${focus}`,
    ...[...new Set(chains)].sort((a, b) => b.length - a.length).slice(0, 8),
    ...transactions.slice(0, 30).map(t => {
      const id = t.id.replace('transaction:tx:', '');
      const suspects = [...(suspectedBy.get(id) ?? [])];
      return `TX ${label(t.id)} ${ref(payer(t.id)?.id)} > ${ref(payee(t.id)?.id)}${flags(t.id)} seen by ${[...(reportedBy.get(id) ?? [])].join('+')}`
        + (suspects.length ? `; suspected by ${suspects.join('+')}` : '');
    }),
    ...accounts.slice(0, 12).map(a => `ACCOUNT ${ref(a.id)}: ${profile(ref(a.id))}`),
    ...accounts.flatMap(a => facts.about(a.id).map(f => `FINDING ${ref(a.id)} ${f.predicate}: ${JSON.stringify(f.value).slice(0, 220)}`)).slice(0, 12),
  ];
  return lines.join('\n').slice(0, 19000);
}

/** Case graph for demos: accounts as nodes, transfers as edges; red = aggregator suspect, orange = specialist-only. */
export function mermaid(world: WorldModel, focus: string, suspectedBy: Map<string, Set<string>>, confirmed: Set<string>): string {
  const graph = world.view('graph');
  const node = (ref: string) => 'a' + ref.replace(/[^A-Za-z0-9]/g, '_');
  const lines = ['flowchart LR'];
  const accounts = graph.query({ type: 'account' }).map(a => a.id.replace('account:acct:', ''));
  for (const ref of accounts) lines.push(`  ${node(ref)}["${ref}"]`);
  lines.push(`  style ${node(focus)} fill:#dbeafe,stroke:#1d4ed8,stroke-width:2px`);
  const edges: string[] = [];
  graph.query({ type: 'transaction' })
    .sort((a, b) => String(a.attrs.time).localeCompare(String(b.attrs.time)))
    .forEach(t => {
      const id = t.id.replace('transaction:tx:', '');
      const from = graph.incoming(t.id, { via: 'paid' })[0]?.id.replace('account:acct:', '');
      const to = graph.neighborsOf(t.id, { via: 'credited' }).find(e => e.type === 'account')?.id.replace('account:acct:', '');
      if (!from || !to) return;
      const by = [...(suspectedBy.get(id) ?? [])];
      const tag = confirmed.has(id) ? ' SUSPECT' : by.length ? ` flagged by ${by.join('+')}` : '';
      lines.push(`  ${node(from)} -- "${id} ${t.attrs.amount} ${t.attrs.currency} ${String(t.attrs.time).slice(5, 16).replace('T', ' ')}${tag}" --> ${node(to)}`);
      edges.push(confirmed.has(id) ? 'stroke:#dc2626,stroke-width:3px' : by.length ? 'stroke:#f59e0b,stroke-width:2px' : '');
    });
  edges.forEach((style, index) => { if (style) lines.push(`  linkStyle ${index} ${style}`); });
  return lines.join('\n') + '\n';
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  investigateTeam(process.argv[2] || '').catch(error => { console.error(error.message); process.exitCode = 1; });
}
