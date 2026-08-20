# Conversation.json 面包屑地图方案

> **日期**：2026-08-18
> **性质**：方案文档——定义"不假设schema的conversation.json遍历→面包屑地图→HANDOVER.md编写"工作流
> **背景**：POC-2.7续传机制中，发现基于固定schema的提取方式有根本缺陷——原始做题的conversation.json因prompt限制（"不要写任何文件"）而结构单一，但续传后AI正常使用工具，conversation.json结构复杂得多。不能基于有限样本的schema做先验假设。

---

## §1 问题背景

### 1.1 先验schema的陷阱

我们曾基于50题采样写了`devin-cli-export-conversation.md`（conversation.json的schema文档），普查919道DIRECTION_ERROR题后得出"字段结构稳定，只有4道题有工具调用"的结论。

**这个结论是错的**。918/919题的原始prompt是"直接在TUI中输出证明，不要写任何文件"——强制AI不调工具。所以这些conversation.json里几乎没有tool_calls/observation，**不是因为AI不会调工具，而是因为prompt禁止了**。

续传后prompt改为"把证明写到proof.md文件中"（不禁止工具调用），续传后的conversation.json立刻出现大量工具调用：exec、write、get_output、kill_shell等，每个工具调用后AI会做新的thinking spin分析结果。

**教训**：基于受限样本建立的schema，不能代表不受限场景下的真实结构。未来devin cli版本升级、prompt变化、模型变化，conversation.json的结构都可能变。

### 1.2 核心思路

**把"理解conversation.json结构"和"编写HANDOVER.md"解耦**：

```
conversation.json
      ↓
遍历程序（不假设schema，递归遍历所有节点）
      ↓
面包屑地图（带JSON path的结构化导航索引）
      ↓
编写HANDOVER.md的AI（按地图逐条读取，整理成交接文档）
      ↓
HANDOVER.md
```

遍历程序只负责"这个JSON里有什么、在哪个path、有多大"——不做语义解释。编写HANDOVER.md的AI负责"这些内容意味着什么、怎么整理成交接文档"——不需要知道schema。

**优势**：即使conversation.json的schema变化（新字段、新结构），只要遍历程序能处理任意JSON（这是递归遍历的基本能力），编写AI就不需要更新。schema知识不需要固化在提取程序里。

---

## §2 面包屑地图格式规范

### 2.1 面包屑的定义

每个面包屑（breadcrumb）是conversation.json中一个节点的导航条目，包含：

| 字段 | 说明 | 示例 |
|---|---|---|
| `path` | JSON path（XPath风格的节点定位） | `steps[10].reasoning_content` |
| `type` | 节点的JSON类型 | `str` / `int` / `list` / `dict` / `bool` / `null` |
| `size` | 节点大小（字符串字符数 / 列表元素数 / dict键数） | `55359c` / `list[7]` / `dict[10keys]` |
| `preview` | 值预览（短字段显示完整值，长字段显示前200字符） | `"我将从之前的分析继续..."` |
| `depth` | 在JSON树中的深度（顶层=0） | `3` |
| `index` | 在兄弟节点中的序号（用于时间排序） | `10` |

### 2.2 地图的分层结构

地图分三层，从概览到详情：

**第一层：顶层概览**
```
conversation.json 顶层结构:
  schema_version  [str, 9c]  "ATIF-v1.7"
  session_id  [str, 16c]  "p27-omni-math-004133"
  agent  [dict, 5keys]
    agent.name  [str, 5c]  "devin"
    agent.version  [str, 9c]  "3000.4.25"
    agent.model_name  [str, 12c]  "GLM-5.2 High"
    agent.tool_definitions  [list[29]]  (工具定义，通常不需要读)
    agent.extra  [dict, 2keys]
  steps  [list[18]]  ← 核心内容，18个step
  final_metrics  [dict, 4keys]
```

**第二层：steps数组的每个step概要**
```
steps[0]  source=system  [dict, 5keys]  message=18653c  ← 系统prompt
steps[1]  source=system  [dict, 5keys]  message=775c   ← 工具定义
steps[2]  source=system  [dict, 5keys]  message=32c    ← "You are powered by..."
steps[3]  source=system  [dict, 5keys]  message=353c   ← system_info
steps[4]  source=system  [dict, 5keys]  message=263c   ← available_skills
steps[5]  source=system  [dict, 5keys]  message=7913c  ← rules(AGENTS.md)
steps[6]  source=system  [dict, 5keys]  message=229c   ← rules(overflow)
steps[7]  source=system  [dict, 5keys]  message=55c    ← 工作系统提醒
steps[8]  source=user    [dict, 5keys]  message=59422c ← 用户prompt(含续传内容)
steps[9]  source=system  [dict, 5keys]  message=9931c  ← system(available_skills补充)
steps[10] source=agent   [dict, 10keys] message=170c   reasoning=55359c  tool=exec  obs=1038c  ← thinking spin + 工具调用
steps[11] source=agent   [dict, 10keys] message=85c    reasoning=29204c  tool=exec  obs=2053c  ← 再thinking spin + 再工具调用
steps[12] source=agent   [dict, 10keys] message=29c    reasoning=24845c  tool=exec  obs=1277c
steps[13] source=agent   [dict, 10keys] message=24c    reasoning=11714c  tool=exec  obs=800c
steps[14] source=agent   [dict, 10keys] message=16c    reasoning=4094c   tool=write obs=154c   ← 写proof.md
steps[15] source=agent   [dict, 10keys] message=37c    reasoning=2561c   tool=exec  obs=970c   ← 验证
steps[16] source=agent   [dict, 10keys] message=803c   reasoning=751c    tool=无   obs=无     ← 最终输出
steps[17] source=agent   [dict, 10keys] ...（如果有更多）
```

**第三层：每个agent step的内部字段详情**
```
=== steps[10] (agent) 详情 ===
  steps[10].step_id           [int]     10
  steps[10].timestamp         [str, 32c] "2026-08-18T11:17:21.610296+00:00"
  steps[10].source            [str, 5c]  "agent"
  steps[10].message           [str, 170c] "我将从之前的分析继续。关键发现是..."
  steps[10].model_name        [str, 12c] "GLM-5.2 High"
  steps[10].reasoning_content [str, 55359c] (大字段，需read完整内容)
  steps[10].tool_calls[0].tool_call_id   [str, 30c] "chatcmpl-tool-9d781af1f9e23afa"
  steps[10].tool_calls[0].function_name  [str, 4c]  "exec"
  steps[10].tool_calls[0].arguments.command [str, 898c] (大字段，需read)
  steps[10].observation.results[0].source_call_id [str, 30c] "chatcmpl-tool-..."
  steps[10].observation.results[0].content [str, 1038c] (大字段，需read)
  steps[10].metrics.prompt_tokens     [int] 45806
  steps[10].metrics.completion_tokens [int] 22050
  steps[10].metrics.cached_tokens     [int] 1355
  steps[10].extra.generation_model    [str, 7c] "glm-5-2"
```

### 2.3 大字段标记

地图对每个字段标注是否为"大字段"（需要编写AI用read工具读取完整内容）：

| 标记 | 阈值 | 含义 | 地图中的处理 |
|---|---|---|---|
| `(小字段)` | < 200c | 地图中已包含完整值 | 编写AI直接使用，不需要read |
| `(中字段)` | 200c ~ 5000c | 地图包含前200字符预览 | 编写AI可read，也可只用预览 |
| `(大字段)` | > 5000c | 地图只有长度和前100字符 | 编写AI必须read完整内容 |
| `(超大字段)` | > 50000c | reasoning_content级别的thinking spin | 编写AI应考虑是否需要完整读取，或只读开头/结尾 |

---

## §3 遍历程序设计规范

### 3.1 程序名称和位置

`scripts/conversation_mapper.py`——conversation.json面包屑地图生成器

### 3.2 输入输出

**输入**：一个或多个conversation.json文件路径

**输出**：
- `{conversation.json同目录}/conversation_map.md`——面包屑地图（Markdown格式，供编写AI阅读）
- stdout：地图的摘要统计

### 3.3 遍历算法

```python
def generate_map(json_path):
    """递归遍历conversation.json，生成面包屑地图。"""
    data = json.load(open(json_path))
    
    map_lines = []
    
    # 第一层：顶层概览
    map_lines.append(generate_top_level(data))
    
    # 第二层：steps数组概要
    if 'steps' in data and isinstance(data['steps'], list):
        map_lines.append(generate_steps_overview(data['steps']))
    
    # 第三层：每个agent step的详情
    for i, step in enumerate(data['steps']):
        if step.get('source') == 'agent':
            map_lines.append(generate_step_detail(step, i))
    
    return '\n'.join(map_lines)
```

### 3.4 遍历规则

1. **不假设任何字段名**——遍历程序不知道`reasoning_content`/`tool_calls`/`observation`等字段名，它只是递归遍历JSON树，对每个节点记录path/type/size/preview
2. **按数组顺序遍历**——`steps`数组的顺序就是时间顺序，按顺序生成面包屑
3. **对dict的keys按出现顺序遍历**——不排序，保留JSON原始顺序
4. **对list的所有元素遍历**——不跳过，每个元素都生成面包屑
5. **大字段只记录元数据**——字符串>200c只记录长度和前100字符，不复制完整内容到地图
6. **null和空值也记录**——`null`、空字符串`""`、空列表`[]`都是有效信息

### 3.5 语义提示（非先验假设）

遍历程序不假设schema，但可以基于**字段名的英文语义**提供轻量提示，帮助编写AI快速定位：

```python
SEMANTIC_HINTS = {
    # 这些提示基于字段名的英文含义，不是schema假设
    # 如果conversation.json中出现未知字段名，遍历程序照常记录，只是没有提示
    'reasoning_content': 'thinking内容（AI内部思考）',
    'thinking': 'thinking内容（AI内部思考）',
    'message': '消息内容（TUI输出或用户输入）',
    'tool_calls': '工具调用列表',
    'function_name': '工具函数名',
    'arguments': '工具调用参数',
    'observation': '工具返回结果',
    'results': '结果列表',
    'content': '内容',
    'source': '来源（system/user/agent）',
    'metrics': '指标（token统计等）',
    'completion_tokens': 'completion token数（截断判定用）',
}
```

**关键**：这些提示是"锦上添花"，不是"必需品"。如果字段名不在提示表中，遍历程序照常生成面包屑，只是没有语义提示。编写AI需要自己根据path和preview判断字段的含义。

---

## §4 编写HANDOVER.md的AI如何使用地图

### 4.1 工作流程

```
1. 编写AI收到任务：为{conversation.json路径}生成HANDOVER.md
2. 编写AI运行：python3 scripts/conversation_mapper.py {conversation.json路径}
3. 编写AI读取生成的 conversation_map.md
4. 编写AI根据地图，按steps顺序逐个agent step处理：
   a. 读地图中的step概要，了解这个step有什么字段、各多大
   b. 对大字段（reasoning_content等），用read工具读取完整内容
   c. 对中字段（observation.content等），用read工具读取或使用预览
   d. 对小字段（function_name等），直接使用地图中的值
5. 编写AI按HANDOVER.md的8个章节整理提取的内容
6. 编写AI输出HANDOVER.md
```

### 4.2 地图指导提取决策

地图帮助编写AI做以下决策：

| 决策 | 地图提供的信息 | 编写AI的行动 |
|---|---|---|
| 哪些step是agent step？ | 第二层概要中的`source=agent`标记 | 只处理agent step |
| 哪些step有工具调用？ | 第二层概要中的`tool=exec`等标记 | 提取tool_calls和observation |
| 哪些step是纯thinking spin？ | 第二层概要中的`tool=无`标记 | 只提取reasoning_content |
| 哪个step被截断了？ | 第三层详情中的`completion_tokens`值 | 标记截断位置 |
| 哪个字段需要完整读取？ | 第三层详情中的`(大字段)`标记 | 用read工具读取 |
| thinking spin有多长？ | 第三层详情中的`reasoning_content [str, 55359c]` | 判断是否需要完整读取还是只读开头/结尾 |

### 4.3 面包屑的"按需读取"原则

地图不复制大字段的完整内容——它只告诉编写AI"在哪里、有多大、开头是什么"。编写AI根据HANDOVER.md的需要决定读什么：

- **HANDOVER.md的§3（已确认的结论）**：需要读reasoning_content中AI得出的数学结论 → read完整reasoning_content
- **HANDOVER.md的§5（关键文献）**：需要读observation中web_search的结果 → read observation.results[].content
- **HANDOVER.md的§6（已有的中间产物）**：需要读tool_calls中write的文件内容 → read tool_calls[].arguments.content
- **HANDOVER.md的§7（当前卡在哪里）**：需要读最后一个step的reasoning_content结尾 → read reasoning_content的后半部分

**不是所有字段都需要完整读取**——地图的价值在于让编写AI知道"有什么"，然后按需读取，避免读取所有内容导致上下文爆炸。

---

## §5 与现有方案的关系

### 5.1 与schema文档的关系

| 文档 | 性质 | 角色 |
|---|---|---|
| `devin-cli-export-conversation.md` | 先验schema文档 | 记录已知的conversation.json字段结构，供快速参考 |
| `trajectory-schema.md` | 先验schema文档 | 记录已知的trajectory.jsonl字段结构，供快速参考 |
| `conversation-map.md`（本文件） | 方案文档 | 定义"不假设schema的遍历→地图→HANDOVER.md"工作流 |
| `scripts/conversation_mapper.py` | 遍历程序 | 实现本方案，生成面包屑地图 |
| `scripts/conversation_field_census.py` | 普查程序 | 统计字段出现频率，验证schema完备性 |

**关系**：schema文档是"已知结构的记录"，地图方案是"未知结构的应对"。两者互补——schema文档用于快速参考，地图方案用于处理结构未知或结构变化的conversation.json。

### 5.2 与续传规范文档的关系

续传规范文档（`续传规范文档.md`）定义了HANDOVER.md的标准结构和提取规则。本方案是续传规范文档§6"自动化路径"的实现——用遍历程序+AI替代手动提取。

### 5.3 与POC-2.7的关系

POC-2.7当前的`batch_continue_948.py`使用v1方案（机械拼接reasoning_content），只提取thinking，不提取tool_calls和observation。本方案是v2方案（交接文档）的自动化实现路径——用面包屑地图指导AI生成HANDOVER.md，替代v1的机械拼接。

---

## §6 实现计划

### 6.1 第一步：实现conversation_mapper.py

基于§3的设计规范，实现遍历程序。核心功能：
- 递归遍历任意JSON结构
- 生成三层面包屑地图（顶层概览 / steps概要 / agent step详情）
- 大字段标记（小/中/大/超大）
- 语义提示（基于字段名的轻量提示，非先验假设）

### 6.2 第二步：验证地图的完备性

对多个conversation.json生成地图，验证：
- 原始做题的conversation.json（受限prompt，结构简单）
- 续传后的conversation.json（不受限prompt，结构复杂）
- 未来可能出现的新的conversation.json结构

地图应该能正确处理所有情况，不遗漏任何节点。

### 6.3 第三步：集成到续传流程

在`batch_continue_948.py`中增加v2方案支持：
- 对TRUNCATED_AT_MAX的题，自动调用conversation_mapper.py生成地图
- 把地图和conversation.json路径交给编写HANDOVER.md的AI（subagent）
- AI生成HANDOVER.md后，用HANDOVER.md作为续传prompt启动下一轮

### 6.4 第四步：验证HANDOVER.md质量

对比v1方案（机械拼接）和v2方案（面包屑地图+HANDOVER.md）的续传效果：
- v2方案的续传成功率是否高于v1？
- v2方案的HANDOVER.md是否包含了v1丢失的observation？
- v2方案的续传prompt是否比v1更短（HANDOVER.md是提炼，不是拼接）？

---

## §7 设计约束

1. **遍历程序不假设schema**——不硬编码`reasoning_content`/`tool_calls`/`observation`等字段名，递归遍历任意JSON结构
2. **地图不复制大字段**——大字段只记录元数据（path/type/size/preview），编写AI按需read
3. **地图按时间顺序**——steps数组的顺序就是时间顺序，面包屑按顺序生成
4. **语义提示是可选的**——字段名不在提示表中时，遍历程序照常工作，只是没有提示
5. **地图是Markdown格式**——供编写AI直接阅读，不需要额外的解析程序
6. **编写AI不需要知道schema**——它只需要能读懂地图和按path读取JSON字段
