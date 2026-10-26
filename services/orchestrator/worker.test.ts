import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Markovian } from '../../vendor/meterless/markovian/src/index.ts';
import { HMEM } from '../../vendor/meterless/hmem/src/index.ts';
import { checkpoint } from './worker.ts';

test('official engine stops at terminal business state without reflection dispatch', async () => {
  let calls = 0;
  const engine = new Markovian({ chunkConfig: { chunkSize: 16000, maxChunks: 10, carryoverTokens: 1024, overlapTokens: 0 } });
  const result = await engine.run({ goal: 'case', reflect: false, stepFn: async () => {
    calls += 1;
    return { content: checkpoint({ status: 'completed', state_version: 1 }) };
  } });
  assert.equal(calls, 1);
  assert.equal(result.status, 'completed');
});

test('official H-MEM does not retrieve another case chat', () => {
  const memory = new HMEM();
  memory.add({ content: 'alpha investigation evidence', source: 'case-a', chatId: 'a', confidence: 1 });
  memory.add({ content: 'beta investigation evidence', source: 'case-b', chatId: 'b', confidence: 1 });
  const result = memory.query('investigation evidence', { chatId: 'a', threshold: 0 });
  assert.ok(result.memories.every(item => item.memory.chatId === 'a'));
});


test('case memory recalls digests relevant to an open question, not unrelated ones', () => {
  const memory = new HMEM();
  memory.add({ content: 'route A > B > C via 1,2; gaps [3h]; retained 0.98', source: 'run:x:ev1', chatId: 'x', confidence: 1 });
  memory.add({ content: 'baseline A over 14d: in_count today 2 vs mean 2', source: 'run:x:ev2', chatId: 'x', confidence: 1 });
  const result = memory.query('where did the funds go next route', { topN: 1, chatId: 'x', threshold: 0 });
  assert.equal(result.memories[0].memory.source, 'run:x:ev1');
});

test('world model merges specialists and builds time-ordered chains', async () => {
  const { WorldModel } = await import('../../vendor/meterless/world-model/src/index.ts');
  const { buildContext } = await import('./team.ts');
  const world = new WorldModel({ namespace: 'test-case', storage: 'memory' });
  const source = { kind: 'agent' as const, by: 'specialist:x', at: new Date() };
  const add = async (id: string, from: string, to: string, time: string) => {
    for (const ref of [from, to]) await world.upsertEntity({ type: 'account', externalKey: { system: 'acct', id: ref }, name: ref, source });
    await world.upsertEntity({ type: 'transaction', externalKey: { system: 'tx', id }, source, attrs: { time, amount: '10', currency: 'USD' } });
    await world.relate({ from: `account:acct:${from}`, type: 'paid', to: `transaction:tx:${id}`, validFrom: time, source });
    await world.relate({ from: `transaction:tx:${id}`, type: 'credited', to: `account:acct:${to}`, validFrom: time, source });
  };
  await add('1', 'S:1', 'A:1', '2022-08-04T01:00:00');
  await add('2', 'A:1', 'B:1', '2022-08-04T03:00:00');
  await add('2', 'A:1', 'B:1', '2022-08-04T03:00:00'); // second specialist, same transaction
  await add('3', 'B:1', 'C:1', '2022-08-04T02:00:00'); // earlier than 2: not a continuation
  const context = buildContext(world, 'A:1', new Map([['2', new Set(['up', 'down'])]]));
  assert.match(context, /3 transactions/);
  assert.match(context, /DOWN A:1 > 2\(08-04 03:00 10 USD\) > B:1/);
  assert.doesNotMatch(context, /DOWN [^\n]*> C:1/);
  assert.match(context, /UP A:1 < 1\(08-04 01:00 10 USD\) < S:1/);
  assert.match(context, /seen by up\+down/);
  const flagged = buildContext(world, 'A:1', new Map(), new Map([['2', new Set(['down'])]]));
  assert.match(flagged, /suspected by down/);
  assert.match(flagged, /ACCOUNT A:1: no profile/);
  const { mermaid } = await import('./team.ts');
  const chart = mermaid(world, 'A:1', new Map([['2', new Set(['down'])]]), new Set(['1']));
  assert.match(chart, /aS_1 -- "1 10 USD 08-04 01:00 SUSPECT" --> aA_1/);
  assert.match(chart, /flagged by down/);
  assert.match(chart, /linkStyle 0 stroke:#dc2626/);
});
