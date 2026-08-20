# 旧模式 · batch_problem_runner 操作SOP

> **归档说明**：本文件记录的是batch_problem_runner.py直接操作模式的16个SOP，来自2026-08-12的调试实战。该系统已被管道化系统（pipe/5服务）完全替代，当前不再运行。保留此文档供历史参考。
> **归档时间**：2026-08-12
> **替代系统**：管道化GLM-5.2能力边界Profile系统（见AGENTS.md中对应节）

---

## SOP 1 · 启动批次

```bash
# 1. 确认环境变量
cd /Users/user/glm5.2-math-worktree
set -a; source .env; set +a

# 2. 确认没有旧批次在跑
tmux list-sessions | grep math-continuous  # 应该没有
tmux list-sessions | grep harness-dpb      # 应该没有

# 3. 如果有旧批次，先停掉
tmux kill-session -t math-continuous 2>/dev/null
pkill -f "batch_problem_runner.py run-continuous" 2>/dev/null
tmux list-sessions 2>&1 | grep "harness-dpb" | cut -d: -f1 | while read s; do tmux kill-session -t "$s"; done

# 4. 启动新批次（交互模式，mitmproxy已禁用，proof_in_tui自动判定+thinking自动落盘）
# 新批次：用--label自动生成batch_id
tmux new-session -d -s math-continuous "set -a; source .env; set +a; \
  .venv/bin/python -u xishujuzhen/solver_harness/batch_problem_runner.py run-continuous \
  --feed-tier 1 --concurrency 30 --poll-seconds 60 \
  --max-runtime-seconds 14400 --stall-seconds 900 --stop-on-stall \
  --launch-interval-seconds 1 --label <你的label> \
  2>&1 | tee /Volumes/data/math-agent-glm5.2-tmux-agents-dir/logs/batch-<你的label>.log"

# 继续已有batch：用--batch-id指定（不会创建新batch，只继续feed+launch queued的题）
tmux new-session -d -s math-continuous "set -a; source .env; set +a; \
  .venv/bin/python -u xishujuzhen/solver_harness/batch_problem_runner.py run-continuous \
  --batch-id <已有batch_id> --feed-tier 1 --concurrency 30 --poll-seconds 60 \
  --max-runtime-seconds 14400 --stall-seconds 900 --stop-on-stall \
  --launch-interval-seconds 1 --label <你的label> \
  2>&1 | tee /Volumes/data/math-agent-glm5.2-tmux-agents-dir/logs/batch-<你的label>.log"
```

## SOP 2 · 检查批次健康（启动后90秒）

```bash
cd /Users/user/glm5.2-math-worktree
set -a; source .env; set +a

# 1. 看monitor日志——10个launch是否全部exit=0
grep -E "\[launch\]" /Volumes/data/math-agent-glm5.2-tmux-agents-dir/logs/batch-<label>.log | head -30

# 2. 看状态
.venv/bin/python xishujuzhen/solver_harness/batch_status.py status

# 3. 看活跃度
.venv/bin/python xishujuzhen/solver_harness/batch_status.py active

# 4. 看dead
.venv/bin/python xishujuzhen/solver_harness/batch_status.py dead
```

**判定标准**：
- `running=10, dead=0` → 健康
- `dead>0` → 有问题，看SOP 4
- `active=10, stalled=0` → 全活跃
- `stalled>0` → 不一定是真stall，看SOP 3

## SOP 3 · 验证STALLED是否真stall（batch_status.py的idle可能不准）

batch_status.py显示STALLED不代表真stall——activity_signature基于文件大小hash，pipe-pane可能因缓冲不写数据导致idle虚高。**必须用tmux capture-pane验证**：

```bash
# 抽样3个STALLED的session看pane
for s in $(tmux list-sessions 2>&1 | grep "harness-dpb" | cut -d: -f1 | head -3); do
  echo "=== $s ==="
  tmux capture-pane -t "$s" -p 2>&1 | grep -v '^$' | grep -E "PROOF COMPLETE|Thinking|Context|CANNOT" | tail -3
  echo
done
```

**判定标准**：
- pane显示`Thinking · Xm Ys` → 还在thinking，不是真stall
- pane显示`PROOF COMPLETE` → 已完成，monitor漏判（看SOP 5）
- pane显示`Ask Devin to build features` + 无Thinking → session已结束，可能完成了
- pane空白 → zombie session，看SOP 4

## SOP 4 · 处理dead session / zombie session

```bash
# 1. 自动清理僵尸session
set -a; source .env; set +a
.venv/bin/python xishujuzhen/solver_harness/batch_status.py dead --cleanup

# 2. 如果dead持续出现，检查根因：
#    a. 看launch日志
grep "\[launch\]" /Volumes/data/math-agent-glm5.2-tmux-agents-dir/logs/batch-<label>.log | grep "exit=" | grep -v "exit=0"
#    b. 看dead session的tmux log
cat /Volumes/data/math-agent-glm5.2-tmux-agents-dir/<exp_id>/tmux/tmux.log | tail -30
#    c. 看dead session的launch日志
cat /Volumes/data/math-agent-glm5.2-tmux-agents-dir/<batch_dir>/logs/launch-<exp_id>.log
```

**已知根因清单**（按排查优先级）：
1. **mitmproxy代理坏了** → 已禁用，launch加`--no-mitm`（已硬编码）
2. **`-p`单轮模式秒退** → 已改`--interactive`（已硬编码）
3. **matharena格式不支持** → 已加`columns.problem`解析
4. **API限流** → 降并发，`set-concurrency --concurrency 5`
5. **tmux session被手动kill** → 不要手动kill harness-dpb开头的session

## SOP 5 · 批量落盘PROOF COMPLETE + 清理IDLE

monitor自动判定proof_in_tui，但可能因poll间隔延迟。手动落盘：

```bash
cd /Users/user/glm5.2-math-worktree
set -a; source .env; set +a
.venv/bin/python xishujuzhen/solver_harness/batch_status.py landfall [--batch-id <batch_id>]
```

扫描所有running session的pane，PROOF COMPLETE的kill+标记candidate_solved，IDLE的kill+标记failed_token_limit。

## SOP 6 · 看Solver的thinking内容

```bash
# 方法1：tmux attach（实时看，Ctrl+B D退出）
tmux attach -t <tmux_session_name>

# 方法2：capture-pane（抓当前屏幕，-S -500抓历史500行）
tmux capture-pane -t <tmux_session_name> -p -S -500

# 方法3：看export文件（最终证明输出，ATIF JSON格式）
cat /Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/<exp_id>/exports/conversation.json | python3 -m json.tool | head -100

# 方法4：看thinking_capture文件（Ctrl+O展开后的完整thinking文本）
cat /Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/<exp_id>/tmux/thinking_capture.txt | tail -100
```

**数据完整性表**：

| 层 | 文件 | 有什么 | 没有什么 |
|---|---|---|---|
| **export** | `<exp_id>/exports/conversation.json` | 最终证明输出（ATIF steps）、system/user消息、metrics（token数） | **thinking内容**；**steps时间戳不覆盖完整解题过程**（export可能一次性写入，用tmux_pipe.log mtime提取solve_time更可靠） |
| **pipe** | `<exp_id>/tmux/tmux_pipe.log` | TUI渲染流（ANSI+braille spinner）、UI行（`Thinking · Xm Ys`）；**文件创建/修改时间=solve_time最可靠来源** | **thinking文本内容** |
| **thinking_capture** | `<exp_id>/tmux/thinking_capture.txt` | **完整thinking文本**（Ctrl+O展开后capture-pane抓取） | 无ANSI清理（raw TUI文本） |
| **sessions.db** | `~/.local/share/devin/cli/sessions.db` | 输入消息（system/user） | **assistant输出和thinking** |

> **thinking落盘机制**：`stop_attempt()`调用前自动调`capture_thinking()`——发Ctrl+O展开thinking，capture-pane抓scrollback 5000行，存到`thinking_capture.txt`。已集成到monitor的proof_in_tui判定流程中。

## SOP 9 · 批量扫描所有session的thinking状态

```bash
cd /Users/user/glm5.2-math-worktree
set -a; source .env; set +a
.venv/bin/python xishujuzhen/solver_harness/batch_status.py scan-thinking [--batch-id <batch_id>]
```

输出每个session的PROOF/THINK/IDLE/OTHER/EMPTY状态 + Context token数 + summary计数。

**判定标准**：
- `PROOF_COMPLETE` → 已完成，用`landfall`落盘
- `THINK(Xm Ys)` → 在thinking
- `IDLE` → token_limited空闲，用`landfall`清理
- `OTHER` → 等API响应或在Yapping阶段
- `EMPTY` → zombie session

## SOP 10 · 恢复export=0B的题

```bash
cd /Users/user/glm5.2-math-worktree
set -a; source .env; set +a
.venv/bin/python xishujuzhen/solver_harness/batch_status.py recover-export [--batch-id <batch_id>]
```

从sessions.db提取metadata写入export文件。**注意：证明内容无法恢复**——sessions.db只有输入没有assistant输出。只能记录metadata证明题做过。

**预防export=0B**：monitor的proof_in_tui判定会先`capture_thinking()`再`stop_attempt()`，不立即kill tmux。如果export仍然=0B，证明内容丢失。

## SOP 7 · 停止批次

```bash
# 1. 停monitor
tmux kill-session -t math-continuous

# 2. 停所有solver session
tmux list-sessions 2>&1 | grep "harness-dpb" | cut -d: -f1 | while read s; do tmux kill-session -t "$s"; done

# 3. 确认全部停止
tmux list-sessions | grep -E "math-continuous|harness-dpb"  # 应该没有
```

## SOP 13 · 手动feed题+resume已有batch（2026-08-12实战方法）

> **场景**：需要重跑某些题（如dead_session的130道tier 1题），但monitor的`run-continuous`启动时会先`select_by_progress_for_feed`选初始题——如果tier题已全部processed，返回空直接退出，不launch已有的queued题。

**方法：先手动create_batch feed题，再用`--batch-id` resume**

```bash
cd /Users/user/glm5.2-math-worktree
set -a; source .env; set +a

# 1. 手动feed题到新batch（用Python调create_batch）
.venv/bin/python -c "
import sys; sys.path.insert(0, 'xishujuzhen/solver_harness')
from batch_problem_runner import load_problem_text_from_progress, create_batch
from arango import ArangoClient
import os
c = ArangoClient(hosts=os.environ.get('ARANGO_HOST','http://localhost:8529'))
db = c.db(os.environ['ARANGO_DB'], username=os.environ.get('ARANGO_USER','root'), password=os.environ.get('ARANGO_PASS',''))
# 找需要重跑的题（按status过滤）
need = list(db.aql.execute('''
  FOR r IN devin_problem_runs
    FILTER r.progress_key != null
    FOR p IN problem_extraction_progress
      FILTER p._key == r.progress_key
      FILTER p.difficulty_tier == 1
      FILTER r.status IN [\"dead_session\", \"launch_error\", \"failed_connection\"]
      RETURN DISTINCT {pid: r.problem_id, progress_key: r.progress_key}
'''))
selected = []
for r in need:
    prog = db.collection('problem_extraction_progress').get(r['progress_key'])
    if not prog: continue
    text = load_problem_text_from_progress(prog)
    if text and len(text.strip()) >= 10:
        selected.append({'progress': prog, 'profile': {'_key': r['pid']},
            'problem_file_text': text.strip() + '\n', 'problem_id_override': r['pid'],
            'source_mode': 'continuous_feed', 'case_metadata': {'rerun': True}})
print(f'fed {len(selected)} problems')
create_batch(db, batch_id='dpb-<你的batch_id>', selected=selected, model='glm-5-2',
    concurrency=60, selection={'rerun': True}, max_runtime_seconds=14400, stall_seconds=900)
"

# 2. 用--batch-id resume（monitor会自动launch queued题）
tmux new-session -d -s math-continuous "set -a; source .env; set +a; \
  .venv/bin/python -u xishujuzhen/solver_harness/batch_problem_runner.py run-continuous \
  --batch-id dpb-<你的batch_id> --feed-tier 1 --concurrency 60 --poll-seconds 60 \
  --max-runtime-seconds 14400 --stall-seconds 900 --stop-on-stall \
  --launch-interval-seconds 1 --label <label> \
  2>&1 | tee /Volumes/data/math-agent-glm5.2-tmux-agents-dir/logs/batch-<label>.log"
```

> **monitor已修复**：`--batch-id`模式下如果已有queued题，跳过初始feed直接进monitor循环launch queued题。
