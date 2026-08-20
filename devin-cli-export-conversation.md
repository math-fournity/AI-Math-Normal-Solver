# Devin CLI Export Conversation.json Schema

> **位置**：`--export`参数指定路径，或harness采集的`{trajectory_base}/{exp_id}/exports/conversation.json`
> **格式**：ATIF JSON（schema_version: ATIF-v1.7）
> **生成时机**：`--export`每轮API响应后实时写入（session运行中也有数据）
> **存在率**：harness采集数据中58%（42%的题因export路径不可写或未指定而缺失）
> **基于50题采样+POC-2.7实测**。2026-08-18定稿。

---

## 1. 顶层Schema

```json
{
  "schema_version": "string — 如'ATIF-v1.7'",
  "session_id": "string — Devin CLI的session ID",
  "agent": {
    "name": "string — 如'devin'",
    "version": "string — 如'3000.4.16'",
    "model_name": "string — 如'GLM-5.2 High'",
    "tool_definitions": "list — 工具定义列表（29个工具）",
    "extra": {
      "backend": "string — 如'Windsurf'",
      "permission_mode": "string — 如'Normal'/'dangerous'"
    }
  },
  "steps": "list[Step] — 步骤列表（线性，无树结构）",
  "final_metrics": {
    "total_prompt_tokens": "int",
    "total_completion_tokens": "int",
    "total_cached_tokens": "int",
    "total_steps": "int"
  }
}
```

---

## 2. Step类型（3种source）

### 2.1 system step

```json
{
  "step_id": "int",
  "timestamp": "string — ISO格式",
  "source": "system",
  "message": "string — 系统消息内容",
  "extra": {
    "telemetry": {
      "source": "string — 如'sysprompt'/'system'/'rules'",
      "operation": "string — 如'normal'/'unknown'/'subagent_profiles'/'content'"
    }
  }
}
```

system step的message内容分5类：

| 类型 | telemetry.source | 平均长度 | 内容 |
|---|---|---|---|
| system_prompt | sysprompt | 18653c | "You are Devin, an interactive command line agent..." |
| tool_def | system/sysprompt | 9274c | 工具定义+subagent profiles |
| short | system | 32c | "You are powered by GLM-5.2 High." |
| system_info | system | 361c | `<system_info>`工作目录/平台/OS版本 |
| rules | rules | 9165c | `<rules type="always-on">`全局+项目AGENTS.md |

### 2.2 user step

```json
{
  "step_id": "int",
  "timestamp": "string",
  "source": "user",
  "message": "string — 用户输入",
  "extra": {
    "telemetry": {
      "source": "user",
      "operation": "unknown"
    }
  }
}
```

### 2.3 agent step（最关键）

```json
{
  "step_id": "int",
  "timestamp": "string",
  "source": "agent",
  "message": "string — TUI输出内容（AI给用户看的回复）",
  "model_name": "string — 如'glm-5-2'",
  "reasoning_content": "string — thinking内容（78%的agent step有）",
  "tool_calls": "list[ToolCall] — 工具调用列表（可能为空）",
  "observation": {
    "results": [
      {
        "source_call_id": "string — 对应的tool_call_id",
        "content": "string — 工具返回结果"
      }
    ]
  },
  "metrics": {
    "prompt_tokens": "int",
    "completion_tokens": "int",
    "cached_tokens": "int"
  },
  "extra": {
    "generation_model": "string — 如'glm-5-2'",
    "telemetry": {
      "source": "assistant",
      "operation": "inference"
    }
  }
}
```

---

## 3. agent step字段统计（89个agent step采样）

| 字段 | 存在率 | 说明 |
|---|---|---|
| step_id | 100% | 步骤ID |
| timestamp | 100% | 时间戳 |
| source | 100% | 固定为"agent" |
| message | 100% | TUI输出，长度0-5670 chars，avg=508 |
| model_name | 100% | 模型名 |
| reasoning_content | 78% | thinking内容，长度0-78128 chars，avg=11170 |
| tool_calls | 100% | 工具调用列表（可能为空列表） |
| observation | 67% | 工具返回结果（有tool_call时才有） |
| metrics | 100% | token统计 |
| extra | 100% | 额外信息 |

**注意**：`reasoning_content`不是所有agent step都有（78%）——有些agent step只有tool_call没有thinking（如执行命令后的等待步骤）。

---

## 4. ToolCall结构

```json
{
  "tool_call_id": "string — 如'chatcmpl-tool-a97adccc6e64f331'",
  "function_name": "string — 工具名",
  "arguments": {
    "command": "string — 命令内容（exec工具）",
    "file_path": "string — 文件路径（read/write/edit工具）",
    "content": "string — 文件内容（write工具）",
    "old_string": "string — 被替换文本（edit工具）",
    "new_string": "string — 替换后文本（edit工具）",
    "shell_id": "string — shell ID（get_output工具）",
    "timeout": "int — 超时毫秒（get_output工具）"
  }
}
```

### arguments按工具名详细结构

| 工具名 | arguments字段 | 类型 | 说明 |
|---|---|---|---|
| exec | command | str | 命令内容 |
| read | file_path | str | 文件路径 |
| write | file_path | str | 文件路径 |
| write | content | str | 文件内容 |
| edit | file_path | str | 文件路径 |
| edit | old_string | str | 被替换的文本 |
| edit | new_string | str | 替换后的文本 |
| get_output | shell_id | str | shell ID |
| get_output | timeout | int | 超时（毫秒） |

### tool_call分布（60个tool_call采样）

| 工具名 | 次数 | 说明 |
|---|---|---|
| exec | 32 | 执行命令 |
| read | 11 | 读取文件 |
| write | 10 | 写入文件 |
| edit | 4 | 编辑文件 |
| get_output | 3 | 获取shell输出 |

**每个agent step最多1个tool_call**（min=1, max=1, avg=1.0）。

---

## 5. observation结构（tool_results的存储位置）

```json
{
  "results": [
    {
      "source_call_id": "string — 对应tool_call_id",
      "content": "string — 工具返回结果"
    }
  ]
}
```

- 每个observation有1个result（min=1, max=1, avg=1.0）
- result的content长度：42-1542 chars，avg=417
- **observation是tool_call返回结果的存储位置**——在conversation.json中，tool_results放在agent step的observation字段中，不在单独的tool step中

---

## 6. steps数量分布

| 指标 | min | max | avg |
|---|---|---|---|
| steps总数 | 8 | 48 | 10 |
| agent_steps数 | 1 | 43 | 3 |

- 大部分题只有1个agent step（单轮thinking spin）
- 少数题有多达43个agent step（多轮thinking spin + 工具调用）

---

## 7. 截断判定标准（POC-2.6/2.7使用）

```python
def is_truncated(export_path):
    d = json.load(open(export_path))
    steps = [s for s in d.get("steps", []) if s.get("source") == "agent"]
    if not steps:
        return False, "no agent step"
    last = steps[-1]
    rc = len(last.get("reasoning_content", "") or "")
    msg = len(last.get("message", "") or "")
    tc = len(last.get("tool_calls", []) or [])
    comp = (last.get("metrics", {}) or {}).get("completion_tokens", 0)
    # 截断：有thinking但没有working产出，且completion_tokens接近上限
    truncated = rc > 1000 and msg == 0 and tc == 0 and comp >= 24000
    return truncated, f"rc={rc}c, msg=0, tc=0, comp={comp}"
```

| 指标 | 截断 | 完成 |
|---|---|---|
| reasoning_content | >1000字符 | 任意 |
| message | =0 | >0 |
| tool_calls | =0 | >0 |
| completion_tokens | ≥24000 | 任意 |

---

## 8. 与trajectory.jsonl的对比

| 维度 | conversation.json | trajectory.jsonl |
|---|---|---|
| 格式 | ATIF JSON（steps数组） | JSONL（每行一个node） |
| agent step记录 | 无重复 | 每个step重复2次（node_id不同，内容相同） |
| thinking字段名 | `reasoning_content` | `thinking` |
| tool_results位置 | agent step的`observation`字段 | 独立的`role="tool"`行，`content`字段 |
| tool_call函数名字段 | `function_name` | `name`（在tool_calls数组中） |
| 树结构 | 无（线性steps） | 有（`node_id`/`parent_node_id`） |
| 生成时机 | `--export`每轮实时写 | session结束后从sessions.db导出 |
| 存在率 | 58% | 82% |

**续传文档构建用conversation.json**——有observation（tool_results），无重复，线性结构易于处理。

---

## 9. 29个工具定义（agent.tool_definitions）

```
ask_user_question, todo_write, mcp_read_resource, mcp_call_tool, edit,
write_to_process, browser_preview, request_scope, read_subagent, apply_patch,
grep, read, notebook_edit, run_subagent, find_file_by_name, close_browser_preview,
kill_shell, exit_plan_mode, webfetch, notebook_read, write, get_output,
mcp_list_servers, skill, shell_command, exec, update_plan, web_search, mcp_list_tools
```

---

## 10. 完整读取代码

```python
import json

with open(export_path) as f:
    d = json.load(f)

# 获取所有agent step
agent_steps = [s for s in d["steps"] if s["source"] == "agent"]

# 提取thinking + tool_calls + observation
for s in agent_steps:
    thinking = s.get("reasoning_content", "") or ""
    message = s.get("message", "") or ""
    tool_calls = s.get("tool_calls", []) or []
    observation = s.get("observation", {}) or {}
    results = observation.get("results", []) or []
    metrics = s.get("metrics", {}) or {}

    for tc, result in zip(tool_calls, results):
        fn = tc.get("function_name", "?")
        args = tc.get("arguments", {})
        obs = result.get("content", "")
        print(f"  [{fn}] args={args} -> {obs[:100]}")
```
