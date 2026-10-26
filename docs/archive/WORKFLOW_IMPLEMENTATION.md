# 执行循环与查询示例

配合 [核心方案](FINAL_TECHNICAL_PLAN.md) 阅读。这里给开发者明确的接入位置，不是已经运行成功的应用代码。Meterless 类名和方法签名已对照官方参考源码；其余函数和表名均是我们要实现的业务适配层。

## 1. Meterless 接入代码应该放哪里

固定上游版本，保留 `engines/hmem/reference/src/` 和 `engines/markovian/reference/src/` 的原始实现。不要把它们当成未经验证的同名 npm 包安装。下面的 vendor 路径是项目自己的目录约定。

```ts
import { Markovian } from "../vendor/markovian/reference/src/index.ts";
import { HMEM } from "../vendor/hmem/reference/src/index.ts";

// C1 是已校验的本案运行标识。每个测试 run 使用独立目录和 chatId。
const scope = "C1_snapshot1_run1";
const memory = new HMEM({ persistDir: `./state/memory/${scope}` });
const engine = new Markovian({
  chunkConfig: {
    chunkSize: 4000,
    carryoverTokens: 1024,
    overlapTokens: 0,
    maxChunks: 24
  },
  historyDir: `./state/markovian/${scope}`
});

const run = await engine.run({
  goal: "调查本案资金关系，验证可疑结构并列出证据缺口",
  mode: "RESEARCH",
  reflect: false,
  stepFn: async ({ carryover }) => {
    // 从数据库取得精确状态，不依赖压缩摘要保存交易或 visited。
    const state = await loadCaseState(scope);
    const recall = memory.query("本案已确认事实、未查分支和下一步问题", {
      chatId: scope, topN: 5
    });

    // 调用真实模型，输出核心方案中的结构化 action。
    const action = await decideNextAction({ state, carryover,
      memoryContext: recall.context, tools: allowedTools });
    validateAction(action, state);

    if (action.action === "finish") {
      const report = await validateAndSaveReport(state, action);
      return { content: report.publicSummary + "\n[TASK_COMPLETE]" };
    }

    // Tool Router 在这里映射固定模板，真正请求 Zetaris。
    const observation = await executeTool(action, state);
    const saved = await persistObservationAndUpdateState(
      scope, action, observation
    );
    memory.add({
      chatId: scope, type: "factual", layer: "working",
      content: saved.verifiedSummary,
      source: saved.evidenceUri,
      provenance: {
        origin: "model_response", learnedAt: Date.now(),
        label: "已经核对工具结果的本步摘要",
        refs: saved.evidenceIds
      }
    });
    // 摘要由应用核实后构造，不任由模型把推测写成已完成事实。
    return {
      content: saved.publicStepSummary + "\n[STATE_CHECKPOINT]\n" +
        "COMPLETED: " + saved.completedSummary + "\n" +
        "REMAINING: " + saved.openQuestionsSummary + "\n" +
        "DECISIONS: " + saved.decisionSummary + "\n" +
        "CONTEXT: " + saved.nextContextSummary
    };
  }
});

if (run.status !== "completed") {
  await markIncomplete(scope, run.status); // 不当作调查成功
}
```

工程上还需分别限制工具总次数、已读取边数和总时间；maxChunks 只是执行段上限。摘要文本应转义控制标记，避免原始数据或模型说明意外包含完成标记而提前结束。

这个接法直接利用 Markovian 已有调度、carryover、进度和历史，不要求重写它的 orchestrator。参考实现中 optional NEEDS_TOOL 标记的说明不等于实际内置了 Zetaris 执行器，因此选择明确可用的 stepFn 接口完成查询。

`decideNextAction` 每次只调用模型选一个动作：系统提示规定允许动作、不能访问标签、不能推断未知身份；输入包含本案状态和实际观察摘要；结构化输出有 action、参数、短理由和 evidence_ids。`executeTool` 只执行白名单工具，返回结果给下一步，模型不能把任意字符串当 SQL 执行。

## 2. Tool Router 到 Zetaris 的对应关系

| 动作 | 工具函数（我们实现） | SQL / 本地算法 |
|---|---|---|
| inspect_account | getAccountActivity | 交易表按账户与时间筛选入账/出账 |
| expand_path | getNextTransfers | 用 anchor 交易关联接收方后续转出 |
| fetch_more | fetchQueryPage | 继续当前 Zetaris query token / cursor |
| check_structure | validateObservedGraph | 对已保存的交易运行确定性结构检查 |
| finish | finalizeCase | 联查证据和政策出处，验证报告引用 |

所有 SQL 仅返回明列的业务字段。query_id、快照、完整性及源行号在工具结果中保留。REST 打开/分页/关闭端点、认证字段使用实际租户文档，不能直接从本文复制一个猜测的 URL。

### 查询 A：初筛 fan_out

以下是查询模板示意，实际 SQL 方言由 Zetaris 环境验收。冒号参数由我们的适配器绑定或严格编码，不表示 REST 原生支持此绑定语法。

```sql
SELECT src_key AS focal_account,
       COUNT(DISTINCT dst_key) AS counterparties
FROM LAKE.transactions
WHERE snapshot_id = :snapshot
  AND event_time >= :window_start
  AND event_time < :scan_time
  AND payment_format IN ('ACH', 'Wire')
  AND src_key <> dst_key
GROUP BY src_key
HAVING COUNT(DISTINCT dst_key) >= :min_counterparties;
```

fan_in 交换 src/dst。支持边另用同样过滤条件按命中账户取回，不能计数用一个窗口、证据却用另一个窗口。当天是否新增支持边由程序检查，纯历史窗口没有当天相关边时不建立当天新告警。

### 查询 B：沿一笔 A→B，查 B 后续转出

```sql
SELECT b.tx_id, b.event_time, b.src_key, b.dst_key,
       b.amount_paid, b.payment_currency,
       b.amount_received, b.receiving_currency,
       b.source_row_number
FROM LAKE.transactions a
JOIN LAKE.transactions b
  ON a.snapshot_id = b.snapshot_id
 AND a.dst_key = b.src_key
WHERE a.snapshot_id = :snapshot
  AND a.tx_id = :anchor_tx_id
  AND a.event_time >= :investigation_start
  AND b.event_time > a.event_time
  AND b.event_time < :case_as_of
  AND b.src_key <> b.dst_key
ORDER BY b.event_time, b.tx_id;
```

服务端必须验证 anchor_tx_id 属于本案已查询证据，investigation_start 和 case_as_of 不由模型放宽。上例只接受严格后续时间；同分钟交易另列顺序不明，不能纳入已确认按时序路径。不同币种金额不直接加总。

Zetaris 分页读完才返回 complete=true。若为节约预算主动截断，明确返回 truncated=true，后续可 fetch_more，也可结束为证据不足。

### 查询 C：跨数据来源得到最终证据

```sql
SELECT e.claim_id, e.query_id,
       t.tx_id, t.event_time, t.src_key, t.dst_key,
       r.rule_id, r.version,
       p.paragraph_id, p.section, p.paragraph_text
FROM APP.evidence_links e
JOIN APP.cases c ON c.case_id = e.case_id
JOIN APP.alerts a ON a.alert_id = c.alert_id
JOIN APP.rules r
  ON r.rule_id = a.rule_id AND r.version = a.rule_version
JOIN APP.policy_paragraphs p
  ON p.paragraph_id = r.source_paragraph_id
JOIN LAKE.transactions t
  ON t.tx_id = e.tx_id AND t.snapshot_id = c.snapshot_id
WHERE c.case_id = :case_id;
```

APP 对应 PostgreSQL，LAKE 对应对象存储交易，均由 Zetaris 注册。案件可能合并多个告警，正式表结构用 case_alerts 关联表保留全部；本例只展示主告警的来源链。程序验证器产生的结构证据还须引用其输入交易集合与检查结果，不把“查到几笔交易”当作验证了闭环。

## 3. 本步结果如何变成下一步输入

工具返回原始记录后先保存完整结果，然后产生有界观察：

```json
{
  "query_id": "Q2",
  "action": "expand_path",
  "anchor_tx_id": "TX_123",
  "complete": true,
  "truncated": false,
  "new_transaction_ids": ["TX_124", "TX_125"],
  "new_accounts": ["bank_C:account_C", "bank_D:account_D"],
  "possible_return_to_visited_account": true,
  "evidence_ids": ["E2", "E3"]
}
```

这里也是格式示例，不是实际样本。下一次模型看到的是“查到了哪些新边、哪些问题仍未解决”，可以选择 check_structure，或继续扩展未查账户。`possible_return_to_visited_account` 仅表示需要检查，不自动成为闭环结论。

状态至少保存：已观察交易、已查账户/方向/窗口、分页状态、待查账户、候选路径、已验证结构、短理由和剩余预算。记忆只保存带来源的摘要，精确状态不能被上下文压缩丢弃。

## 4. 参考实现依据与限制

- [Markovian facade](https://github.com/Meterless/Meterless/blob/main/engines/markovian/reference/src/index.ts)：run、onProgress。
- [Markovian types](https://github.com/Meterless/Meterless/blob/main/engines/markovian/reference/src/types.ts)：stepFn、ChunkConfig、RunOptions。
- [实际 orchestrator](https://github.com/Meterless/Meterless/blob/main/engines/markovian/reference/src/orchestrator.ts)：逐步调用、carryover、停止检测。
- [H-MEM facade](https://github.com/Meterless/Meterless/blob/main/engines/hmem/reference/src/index.ts)：add、query。
- [Zetaris SQL](https://kbase.zetaris.com/knowledge/sql-manual)、[REST 查询](https://kbase.zetaris.com/knowledge/rest-api)。

本次已读源码核对接口，未运行真实模型或赞助商租户；代码示意中的业务函数仍需实现。Markovian 参考引擎默认以字符数估算 token，不将估算称为实测成本节约。
