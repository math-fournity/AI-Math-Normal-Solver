---
description: >
  树生长实验操作流程——运行POC-VMS实验时加载此skill。
  包含实验前检查清单、实验中辅助Pipe操作流程、实验后验证清单。
  对应274号Checklist第5节"辅助智能体角色执行Checklist"。
  WHEN to use: 运行POC-VMS实验、启动树生长引擎、任何涉及两棵树生长的实验。
  WHEN NOT to use: 单AI串行解题（非树生长模式）、非数学实验。
---

# 树生长实验操作流程

## 实验前检查清单

- [ ] 确认`echo $ARANGO_DB`输出`xishujuzhen_math_glm52`
- [ ] 确认mitmproxy在运行：`lsof -i :18889 | grep mitmdump`
- [ ] 确认thinking实时数据路径：`ls /Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/_shared/mitm_raw/thinking_live.txt`
- [ ] 确认并发额度：最多2个AI（`.devin/rules/solver-concurrency.md`）
- [ ] 确认数据库集合存在：tree_nodes/tree_edges/problems/ai_instances/patterns
- [ ] 确认测试题文件已生成
- [ ] 确认检索器（retriever.py）能从patterns集合检索到方向

## 实验中辅助Pipe操作流程

**你是辅助智能体（系统Pipe），不是旁观者。推理AI在工作时你也在工作。**

### 步骤1：启动第一批AI（最多2个）

```bash
.venv/bin/python3 xishujuzhen/solver_harness/solver_harness.py launch --exp-id <exp_id> --problem-file <problem_file>
```

### 步骤2：实时采集+整理树（AI一边工作，系统一边整理）

每10秒轮询一次thinking_live.txt：
- 读`/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/<exp_id>/mitm/thinking_live.jsonl`
- 按counter累积chunks得到完整round文本
- 用node_extractor.py提取节点
- 用tree_store.py写入ArangoDB（推动关系2）

**如果你发现自己在sleep或等待——停。你违反了并行运行原则。回到采集/整理。**

### 步骤3：AI终止后在叶节点检索+启动新AI（推动关系3）

AI终止时：
1. 获取其最后一个节点（叶节点）
2. 在叶节点上调用`retriever.py`检索方向Q
3. 标记叶节点`retrieval_done=True`，记录`directions_identified`
4. 对每个方向，构造脉络（从根到叶节点的路径+方向Q）
5. 将新AI任务加入task_queue（受2并发约束）
6. 新AI启动后，从叶节点创建新边（引导树长出新边）

### 步骤4：检查停机

- 检测AI最终输出是否包含"证毕"/"QED"/"boxed"
- 如果包含，标记为leaf_success，problem标记为solved
- problem solved后不再从该树的叶节点启动新AI

### 步骤5：循环直到所有problem solved或exhausted

```
while task_queue or running_ais:
    填满并发额度（最多2个）
    实时采集+整理树（步骤2）
    AI终止后检索+启动新AI（步骤3）
    检查停机（步骤4）
    sleep(POLL_INTERVAL)  # 10秒
```

## 实验后验证清单

- [ ] 验证tree_nodes有节点、tree_edges有边
- [ ] 检查循环完整性：三个推动关系是否都成立
  - 推动关系1：有脉络的AI数 > 0
  - 推动关系2：有thinking片段的节点数 > 0
  - 推动关系3：叶节点retrieval_done=True的数量 > 0，从叶节点启动的新AI数 > 0
- [ ] 如果只有节点没有边（循环没转完整），记录原因
- [ ] 统计：节点数、边数、分叉数、树深度、solved率
- [ ] 落盘评估报告到dev-docs

## 循环完整性诊断SQL

```python
# 推动关系1：引导树→推理AI
guided = list(db.aql.execute('FOR a IN ai_instances FILTER a.hint_q != null RETURN a._key'))

# 推动关系2：推理AI→解题树
with_traj = list(db.aql.execute('FOR n IN tree_nodes FILTER n.trajectory_segment.thinking != null RETURN n'))

# 推动关系3：推理AI→引导树
retrieval_done = list(db.aql.execute('FOR n IN tree_nodes FILTER n.node_type IN ["leaf_success","leaf_deadend","leaf_truncated"] FILTER n.retrieval_done == true RETURN n'))

# 判定
if len(retrieval_done) == 0:
    print("❌ 循环只转了半圈——解题树在长，但叶节点没有检索，没有启动新AI")
else:
    print(f"✅ 循环完整——{len(retrieval_done)}个叶节点做了检索")
```

## 关联文档

- `dev-docs/273-v0-2026-08-08-Grove核心循环与辅助智能体JD-跨AI认知同步.md`
- `dev-docs/274-v0-2026-08-08-后续工作Checklist-让核心循环完整转起来.md`
- `.devin/rules/grove-core-loop.md`
- `.devin/rules/solver-concurrency.md`
