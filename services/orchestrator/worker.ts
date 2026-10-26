/** Official engines handle chunks and memory; the API owns business state. */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { Markovian } from '../../vendor/meterless/markovian/src/index.ts';
import { HMEM } from '../../vendor/meterless/hmem/src/index.ts';
import { API_BASE, apiClient, apiToken, root } from './http.ts';

const terminal = new Set(['completed', 'insufficient_evidence', 'budget_exhausted', 'model_unavailable', 'tool_failed', 'cancelled']);

interface Observation { evidence_ref: string; question_id?: number | null; digest?: string[] }
interface Question { id: number; text: string; status: string }

interface RunState {
  id: string;
  status: string;
  state_version: number;
  request: { split: string; as_of: string };
  observations: Observation[];
  questions?: Question[];
  plan: string[];
  budget: Record<string, number>;
}

export function checkpoint(state: {status: string; state_version: number; observations?: unknown[]; plan?: string[]}): string {
  if (terminal.has(state.status)) return `[TASK_COMPLETE]\n${JSON.stringify({ status: state.status, version: state.state_version })}`;
  return `[STATE_CHECKPOINT]\nCOMPLETED: ${state.observations?.length ?? 0} observations\nREMAINING: ${JSON.stringify(state.plan)}\nDECISIONS: state ${state.state_version}\nCONTEXT: authoritative evidence remains in business API`;
}

export async function runCase(runId: string): Promise<void> {
  if (!/^[a-f0-9-]{36}$/.test(runId)) throw new Error('Expected run UUID');
  const workerId = randomUUID();
  const profile = JSON.parse(fs.readFileSync(path.join(root, 'docs/architecture/AML_Agent_Config_v1.0.json'), 'utf8'));
  // One step may hold a model call plus repair and two SQL requests; wait for all of them.
  const stepTimeoutMs = (2 * profile.provider.timeout_seconds
    + 2 * profile.tools.sql_client_response_timeout_seconds_not_server_cancellation + 60) * 1000;
  const request = apiClient(API_BASE, apiToken(), stepTimeoutMs);
  const lease = await request<{fence: number}>(`/internal/jobs/${runId}/claim`, { worker_id: workerId });
  let state = await request<RunState>(`/v1/runs/${runId}`);
  // Per-run and per-split isolation is enforced by separate persistent stores.
  const memoryDir = path.join(root, '.runtime/memory', state.request.split, runId);
  const memory = new HMEM({ persistDir: memoryDir, actor: 'aml-worker' });
  // Case memory holds code-generated observation digests (not state snapshots),
  // one record per evidence reference, so a restart never duplicates them.
  function syncMemory(current: RunState): void {
    const known = new Set(memory.store.all().map(record => record.source));
    for (const observation of current.observations) {
      const source = `run:${runId}:${observation.evidence_ref}`;
      if (known.has(source) || !observation.digest?.length) continue;
      memory.add({ content: observation.digest.join('\n'), source, confidence: 1, chatId: runId, goalRunId: runId,
        tags: [current.request.split, `q${observation.question_id ?? 0}`, 'observation-digest'] });
    }
  }

  // Recall older facts for the questions still open; the latest two observations
  // are already in the working context, so they are excluded here.
  function recall(current: RunState): string {
    const open = (current.questions ?? []).filter(q => q.status === 'open').map(q => q.text).join(' ');
    const recent = new Set(current.observations.slice(-2).map(o => `run:${runId}:${o.evidence_ref}`));
    if (!open || current.observations.length <= 2) return '';
    const result = memory.query(open, { topN: 8, chatId: runId, threshold: 0 });
    return result.memories
      .filter(item => !recent.has(item.memory.source))
      .slice(0, 5)
      .map(item => item.memory.content)
      .join('\n')
      .slice(0, 3200);
  }
  syncMemory(state);
  const engine = new Markovian({
    chunkConfig: { chunkSize: profile.markovian.chunkSize, maxChunks: profile.markovian.maxChunks,
      carryoverTokens: profile.markovian.carryoverTokens, overlapTokens: profile.markovian.overlapTokens },
    historyDir: path.join(root, '.runtime/engine-history', runId),
  });
  let renewing = false;
  let leaseError: Error | null = null;
  const timer = setInterval(async () => {
    if (renewing) return;
    renewing = true;
    try { await request(`/internal/jobs/${runId}/renew`, { worker_id: workerId, fence: lease.fence }); }
    catch (error) { leaseError = error as Error; }
    finally { renewing = false; }
  }, 30_000);
  try {
    const result = await engine.run({
      goal: 'Investigate this case using bounded observations and produce an evidence-bound conclusion.',
      reflect: profile.markovian.reflect,
      stepFn: async () => {
        if (leaseError) throw leaseError;
        state = await request<RunState>(`/v1/runs/${runId}`);
        if (terminal.has(state.status)) return { content: checkpoint(state) };
        state = await request<RunState>(`/internal/runs/${runId}/step`, {
          expected_state_version: state.state_version, action_id: `step_${state.state_version}`,
          worker_id: workerId, fence: lease.fence, memory: recall(state),
        });
        // Reconcile the durable business log after every step. On a memory
        // failure a restart reads state; completed SQL is never re-executed.
        syncMemory(state);
        console.log(JSON.stringify({ run_id: runId, status: state.status, state_version: state.state_version, budget: state.budget }));
        return { content: checkpoint(state) };
      },
      stopWhen: () => terminal.has(state.status),
    });
    console.log(JSON.stringify({ run_id: runId, engine_status: result.status, case_status: state.status,
      engine_token_label: result.tokenLabel, provider_budget: state.budget }));
  } finally {
    clearInterval(timer);
  }
  if (!['completed', 'insufficient_evidence'].includes(state.status)) {
    throw new Error(`Case stopped with ${state.status}; engine completion is not business success`);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  runCase(process.argv[2] || '').catch(error => { console.error(error.message); process.exitCode = 1; });
}

