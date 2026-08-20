# Trajectory.jsonl Schema（sessions_db导出）

> **位置**：`{trajectory_base}/{exp_id}/sessions_db/trajectory.jsonl`
> **格式**：JSONL，每行一个JSON对象
> **生成时机**：session结束后从sessions.db（SQLite）导出
> **存在率**：harness采集数据中82%（高于conversation.json的58%）
> **基于50题采样+POC-2.7实测**。2026-08-18定稿。

---

## 1. 单行Schema

```json
{
  "type": "string — 'step'",
  "node_id": "int — 节点ID",
  "parent_node_id": "int — 父节点ID（构成树结构）",
  "role": "string — 'user'/'assistant'/'tool'",
  "content": "string — 消息内容（user/assistant的文本，tool的工具返回结果）",
  "thinking": "string — thinking内容（仅assistant角色有，user/tool为空字符串）",
  "tool_calls": "list — 工具调用列表（仅assistant角色有，user/tool为空列表）",
  "created_at": "int — Unix时间戳"
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| type | string | 固定为"step" |
| node_id | int | 节点唯一标识 |
| parent_node_id | int | 父节点ID，构成DFS树结构 |
| role | string | user/assistant/tool 三种 |
| content | string | user: 用户输入；assistant: TUI输出（对应conversation.json的message）；tool: 工具返回结果 |
| thinking | string | assistant的thinking内容（对应conversation.json的reasoning_content） |
| tool_calls | list | assistant的工具调用列表 |
| created_at | int | Unix时间戳（秒） |

---

## 2. role分布与行数统计（50题采样）

| (type, role) | 出现次数 | 说明 |
|---|---|---|
| (step, user) | 129 | 用户输入 |
| (step, assistant) | 111 | AI回复（含thinking） |
| (step, tool) | 27 | **工具返回结果** |

| 指标 | min | max | avg |
|---|---|---|---|
| 行数 | 1 | 22 | 4 |

---

## 3. 三种role的行结构

### 3.1 user行

```json
{
  "type": "step",
  "node_id": 8,
  "parent_node_id": 7,
  "role": "user",
  "content": "请按AGENTS.md中的题目直接解答...",
  "thinking": "",
  "tool_calls": [],
  "created_at": 1723426800
}
```

### 3.2 assistant行

```json
{
  "type": "step",
  "node_id": 23,
  "parent_node_id": 22,
  "role": "assistant",
  "content": "## Proof\n\n**Setup.** Let $f_K$...",
  "thinking": "Let me solve this optimization problem...",
  "tool_calls": [
    {
      "arguments": {"file_path": "/path/to/problem.txt"},
      "id": "chatcmpl-tool-xxx",
      "index": 0,
      "kind": "function",
      "name": "read"
    }
  ],
  "created_at": 1723426801
}
```

### 3.3 tool行（tool_results的存储位置）

```json
{
  "type": "step",
  "node_id": 25,
  "parent_node_id": 24,
  "role": "tool",
  "content": "<file-view path=\"/path/to/problem.txt\">...</file-view>",
  "thinking": "",
  "tool_calls": [],
  "created_at": 1723426802
}
```

**关键**：tool行的`content`字段就是tool_call的返回结果（observation）。这是与conversation.json的不同之处——conversation.json把tool_results放在agent step的observation字段中，trajectory.jsonl把tool_results放在单独的tool行中。

---

## 4. tool_calls结构（与conversation.json不同）

```json
{
  "arguments": "dict — 参数",
  "id": "string — tool_call_id（如'chatcmpl-tool-xxx'）",
  "index": "int — 序号",
  "kind": "string — 如'function'",
  "name": "string — 工具名（对应conversation.json的function_name）"
}
```

| 字段 | trajectory.jsonl | conversation.json |
|---|---|---|
| 工具名 | `name` | `function_name` |
| tool_call_id | `id` | `tool_call_id` |
| 额外字段 | `index`, `kind` | 无 |

---

## 5. 重复问题（重要）

**每个assistant step在trajectory.jsonl中出现2次**——node_id不同，但content/thinking/tool_calls完全相同。

实测mathnet_001631（10个agent step的截断run）：
- conversation.json: 10个agent step（无重复）
- trajectory.jsonl: 20个assistant行（每个step重复2次，如line1和line2都是33c thinking，node_id 23和24，parent都是22）

**处理建议**：解析trajectory.jsonl时，按`(thinking, content, tool_calls)`三元组去重，或只取每个parent的第一个child。

---

## 6. 树结构

trajectory.jsonl通过`node_id`/`parent_node_id`构成树结构，可以重建DFS树。这对分析AI的探索路径有用，但对续传文档构建不是必需的——续传文档需要的是"AI做了什么、发现了什么、卡在哪"，这是线性叙事。

---

## 7. 与conversation.json的对比

| 维度 | trajectory.jsonl | conversation.json |
|---|---|---|
| 格式 | JSONL（每行一个node） | ATIF JSON（steps数组） |
| 存在率 | 82% | 58% |
| agent step记录 | 每个step重复2次 | 无重复 |
| thinking字段名 | `thinking` | `reasoning_content` |
| tool_results位置 | 独立的`role="tool"`行，`content`字段 | agent step的`observation`字段 |
| tool_call函数名字段 | `name` | `function_name` |
| 树结构 | 有（`node_id`/`parent_node_id`） | 无（线性steps） |
| 生成时机 | session结束后从sessions.db导出 | `--export`每轮实时写 |

### 数据源选择建议

1. **优先用conversation.json**——有observation（tool_results），无重复，线性结构易于处理。58%存在率。
2. **conversation.json不存在时用trajectory.jsonl**——82%存在率，需要处理重复问题，tool_results在tool行中。
3. **两者都不存在时**——只有tmux日志（90%存在率），但只有TUI输出，无thinking。

---

## 8. 完整读取代码

```python
import json

with open(traj_path) as f:
    lines = [json.loads(line) for line in f]

# 按role分组
user_lines = [l for l in lines if l["role"] == "user"]
assistant_lines = [l for l in lines if l["role"] == "assistant"]
tool_lines = [l for l in lines if l["role"] == "tool"]

# 去重assistant行（按thinking+content+tool_calls）
seen = set()
unique_assistant = []
for l in assistant_lines:
    key = (l.get("thinking", ""), l.get("content", ""), json.dumps(l.get("tool_calls", []), sort_keys=True))
    if key not in seen:
        seen.add(key)
        unique_assistant.append(l)

# 关联tool_call和tool_result
# tool行的parent_node_id指向对应的assistant行的node_id
for a in unique_assistant:
    thinking = a.get("thinking", "")
    content = a.get("content", "")
    tool_calls = a.get("tool_calls", [])
    # 找对应的tool结果
    for t in tool_lines:
        if t["parent_node_id"] == a["node_id"]:
            tool_result = t.get("content", "")
            # 关联tool_call和result
```

---

## 9. harness采集的完整trajectory目录结构（参考）

```
{trajectory_base}/{exp_id}/
├── session_info.json              # 元数据（100%存在）
├── exports/
│   └── conversation.json          # ATIF格式export（58%存在）← 见devin-cli-export-conversation.md
├── sessions_db/
│   └── trajectory.jsonl           # SQLite导出的trajectory（82%存在）← 本文件
├── mitm/                           # MITM截获数据（38%存在，仅早期批次，已废弃）
│   ├── thinking_live.jsonl
│   ├── thinking_live.txt
│   ├── thinking_readable.txt
│   └── trajectory.jsonl
└── tmux/
    ├── tmux_pipe.log              # tmux pipe-pane输出（90%存在）
    ├── tmux.log                   # tmux日志（90%存在）
    └── thinking_capture.txt       # thinking截获（常为空）
```

**MITM已废弃**（2026-08-18）：`batch_problem_runner.py`第931行硬编码`--no-mitm`，当前和未来数据没有MITM。thinking数据只能从`sessions_db/trajectory.jsonl`和`exports/conversation.json`获取。

完整目录结构详见：`analysis-devin-failure-system/docs/solver-trajectory-schema.md`
