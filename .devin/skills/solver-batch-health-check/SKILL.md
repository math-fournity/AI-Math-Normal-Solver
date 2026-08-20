# Solver批量集群健康检查SOP

**触发条件**：检查批量Solver集群运行状态时；用户说"看看进度"/"检查有没有问题"时；新session接手批量系统时。
**配套Rule**：`.devin/rules/solver-batch-health-check.md`（铁律+并发经验+已知问题清单）
**检查工具**：管道化系统用`pipe_control.py`，旧batch系统用`batch_status.py`

---

## 管道化系统检查流程（当前生效）

**铁律0：检查不用长sleep等待——简单查询立即执行**

检查命令都是秒级返回的，不需要任何sleep前置。禁止`sleep 300 && pipe_control.py status`这种写法。

**一键检查**（即时返回，不加sleep）：

- `pipe_control.py status`——总状态（服务存活+队列+实时配置）
- `pipe_control.py status -v`——含running attempt详情
- `pipe_control.py health`——全面健康检查（7项+末尾提醒检查Monitor Pipe）
- `tmux list-sessions | grep pipe-`——6个服务session必须都在（含pipe-monitor）
- `tail -10 .../logs/collector.log`——看collector最近处理了什么
- `tail -5 .../logs/reporter.log`——看reporter最近统计

**Monitor Pipe检查**（`pipe_control.py health`输出末尾会提醒）：

- `bash xishujuzhen/solver_harness/pipe/scripts/monitor_check.sh`——标准化检查脚本（5项检查+对AI的核心提醒）
- Monitor Pipe持续监控13项自动检查+AI review抽样，alert写入`pipe_monitor_alerts`集合
- 详见AGENTS.md中"### Monitor Pipe检查"小节和`dev-docs/391号`

**看完全部输出后，按AGENTS.md中"系统运行监控SOP"节的6项检查清单逐项判定。**

---

## 旧batch系统检查流程（已废弃，仅参考）

**一键检查流程**：

```bash
set -a; source .env; set +a
.venv/bin/python xishujuzhen/solver_harness/batch_status.py all
```

输出包含：status（总状态）→ active（活跃度）→ errors（错误分析）→ solved（已解决）→ feed（feed事件）→ leak（泄漏检查）。

**看完全部输出后，按下面的检查清单逐项判定。**

---

## 检查清单（7项，按顺序）

### 检查1：批次是否活着

```bash
ps aux | grep "batch_problem_runner" | grep -v grep
tmux has-session -t math-continuous 2>&1
```

- monitor进程不在 → 重启monitor
- tmux session不在 → 重启monitor

### 检查2：running数是否合理

`batch_status.py status`看attempt计数。running应该≈concurrency。

- running=0但total没到feed_exhausted → **立即查僵尸session**（检查4）
- running远大于concurrency → monitor没在回收，查僵尸session
- running远小于concurrency但feed剩余>0 → feed卡住，查feed日志

### 检查3：活跃度——running的题真的在工作吗

`batch_status.py active`看每个running attempt：
- **ACTIVE**（idle<120s）：正常
- **STALLED**（idle>120s）：可能卡住，看下一步
- **think=0KB且pipe=0KB**：高度可疑——Devin CLI可能已退出

**关键判定**：idle=0s不代表在工作。activity_signature可能因tmux_log_path变化永远在变。必须看thinking_readable_path的实际大小——0KB=没产出。

### 检查4：僵尸session——最关键的检查

```bash
# 批量检查所有running的tmux pane是否空白
set -a; source .env; set +a
.venv/bin/python -c "
import subprocess
from arango import ArangoClient
import os
from pathlib import Path
c = ArangoClient(hosts=os.environ.get('ARANGO_HOST','http://localhost:8529'))
db = c.db(os.environ['ARANGO_DB'], username=os.environ.get('ARANGO_USER','root'), password=os.environ.get('ARANGO_PASS',''))
for a in db.aql.execute('''
  FOR a IN devin_problem_runs
    FILTER a.status IN [\"running\", \"stalled_warning\"]
    RETURN {pid:a.problem_id, tmux:a.tmux_session, paths:a.paths}
'''):
    r = subprocess.run(['tmux','capture-pane','-t',a['tmux'],'-p'], capture_output=True, text=True, timeout=10)
    stripped = ''.join(line.strip() for line in r.stdout.split('\n') if line.strip())
    pipe = Path(a['paths']['tmux_pipe_path'])
    pipe_text = pipe.read_text() if pipe.exists() else ''
    if len(stripped) < 5:
        tag = 'PROOF' if '### PROOF COMPLETE' in pipe_text else 'DEAD'
        print(f'  [{tag}] {a[\"pid\"]}')
"
```

- **DEAD**：tmux pane空白且无PROOF COMPLETE → 僵尸session，手动kill + 判定终态
- **PROOF**：tmux pane空白但有PROOF COMPLETE → 已完成但monitor没判定，手动标solved

### 检查5：错误分类——failed_no_proof是真的做不出来吗

`batch_status.py errors`看marker统计和failed_no_proof拆分。

- **failed_connection**：API连接错误，不是做不出来，重跑可能成功
- **failed_token_limit**：thinking 50-89KB但输出被截断，可能写了大半证明
- **dead_session**：僵尸session，Devin CLI退出但tmux没死
- **真正的failed_no_proof**：有thinking产出但没输出PROOF COMPLETE——只有这种才是真做不出来

**关键认知**：没有一个AI会主动说"我做不出来"。所有失败都是外部中断（连接断/token超限/僵尸）。真正的"做不出来"=有thinking但没PROOF COMPLETE。

### 检查6：feed是否正常

`batch_status.py feed`看feed事件。

- 反复出现`SKIP ... unsupported source format` → 题面提取不支持该格式，需加格式支持
- feed_exhausted → 该tier所有题都有记录了，批次正常完成
- 没有feed事件但running<concurrency → feed逻辑有bug

### 检查7：答案泄漏

`batch_status.py leak`看三层检查结果。

- DB marker=0 + real_leak_reports=0 → 无泄漏
- instruction_text_matches>0 → 正常（AGENTS.md指令文本被export记录）
- real_leak_reports>0 → **真泄漏**，需检查提取逻辑的安全截断

---

## 发现问题后的修复流程

1. **先在batch_status.py中加检测命令**——让下次能一键发现同类问题
2. **再在batch_problem_runner.py中修监控逻辑**——让monitor能自动处理
3. **更新rule中的已知问题清单**——记录问题现象、根因、修复方法
4. **commit**——检查工具修复和监控逻辑修复分开commit

---

## 手动清理僵尸session（紧急操作）

当monitor还没升级到能自动处理僵尸session时，手动清理：

```bash
set -a; source .env; set +a
.venv/bin/python -c "
import subprocess
from arango import ArangoClient
import os
from pathlib import Path
from datetime import datetime, timezone
c = ArangoClient(hosts=os.environ.get('ARANGO_HOST','http://localhost:8529'))
db = c.db(os.environ['ARANGO_DB'], username=os.environ.get('ARANGO_USER','root'), password=os.environ.get('ARANGO_PASS',''))
now = datetime.now(timezone.utc).isoformat()
for a in db.aql.execute('''
  FOR a IN devin_problem_runs
    FILTER a.status IN [\"running\", \"stalled_warning\"]
    RETURN {key:a._key, pid:a.problem_id, tmux:a.tmux_session, paths:a.paths}
'''):
    r = subprocess.run(['tmux','capture-pane','-t',a['tmux'],'-p'], capture_output=True, text=True, timeout=10)
    stripped = ''.join(line.strip() for line in r.stdout.split('\n') if line.strip())
    pipe = Path(a['paths']['tmux_pipe_path'])
    pipe_text = pipe.read_text() if pipe.exists() else ''
    if len(stripped) < 5:
        subprocess.run(['tmux','kill-session','-t',a['tmux']], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if '### PROOF COMPLETE' in pipe_text:
            status = 'candidate_solved'
        elif 'Connection error' in pipe_text or 'unavailable' in pipe_text:
            status = 'failed_connection'
        else:
            status = 'dead_session'
        db.collection('devin_problem_runs').update({'_key':a['key'],'status':status,'ended_at':now,'end_reason':'manual_cleanup_zombie'})
        print(f'  {status}: {a[\"pid\"]}')
"
```

---

## 并发调整

```bash
set -a; source .env; set +a
.venv/bin/python xishujuzhen/solver_harness/batch_problem_runner.py set-concurrency --batch-id <batch_id> --concurrency <N>
```

推荐范围：10-30。不超过30。
