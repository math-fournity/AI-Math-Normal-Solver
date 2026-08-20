# Solver监控操作手册

**触发条件**：Master AI需要观察或干预在tmux中运行的Solver AI时
**来源**：从项目AGENTS.md移出（2026-08-08清理），原"临时章节：如何检查正在工作的Solver AI"

---

## 启动Solver session

```bash
# 1. 创建实验目录（新模式：tmux-agents-dir）
EXP_DIR="/Volumes/data/math-agent-glm5.2-tmux-agents-dir/<experiment-id>"
mkdir -p ${EXP_DIR}/exports
cp templates/solver_agents_md.md ${EXP_DIR}/AGENTS.md  # 从模板复制

# 检查占用
tmux list-sessions | grep -E "solver|bare|guided"

# 2. 写problem.txt到实验目录
# 3. 用tmux启动devin cli
tmux new-session -d -s <session-name> "cd ${EXP_DIR} && devin"
sleep 8
tmux capture-pane -t <session-name> -p | tail -15  # 检查是否启动

# 4. 如果出现trust prompt，选择"Yes, trust"
tmux send-keys -t <session-name> "1" Enter

# 5. 启动pipe-pane兜底记录（写入实验目录）
tmux pipe-pane -t <session-name> "cat >> ${EXP_DIR}/tmux_pipe.log"

# 6. 发送题目指令
tmux send-keys -t <session-name> "请读取当前目录下的problem.txt文件，然后做题。" Enter
```

> **注意**：正式实验应通过 `xishujuzhen/solver_harness/solver_harness.py launch` 启动（硬约束4），上面是手动观察用。

## 观察Solver工作过程

```bash
# 基本观察（看最后30行）
tmux capture-pane -t <session-name> -p -S -100 | tail -30

# 深度观察（看最后300行，去掉ANSI噪音）
tmux capture-pane -t <session-name> -p -S -300 | grep -v '^\[' | grep -v '^$' | tail -60

# 检查AI是否写了文件
ls -la ${EXP_DIR}/proof* 2>/dev/null

# 检查AI的thinking字符数（判断思考深度）
# 屏幕上会显示 "Thinking · Xm Ys · (NNNNNc · ctrl+o for details)"
# NNNNNc是thinking的字符数，40k+表示深度思考
```

## 已知问题与处理方法

### 问题1：Response truncated（输出被截断）

**现象**：AI在thinking阶段花40-50k字符思考，然后在output阶段一次性输出完整证明文本，达到max output token limit被截断。

**根因**：GLM-5.2倾向于"想完所有内容然后一次性输出文本"，不主动调用write/exec工具。

**处理方法**：
1. **预防**：在初始指令中就明确要求"把证明写到文件里，不要在对话里输出证明内容。用write工具写proof.md"
2. **预防**：指令要简短——"只做第一问"比"两问都要做"更容易让AI不触发截断
3. **预防**：告诉AI"用python3 -c命令把证明写到文件"——exec工具比write工具更容易被AI调用
4. **截断后**：发"continue"可能再次截断。更好做法是杀掉session重新开始，用更简短的指令
5. **最有效**：让AI先做数值验证（exec工具），验证完后它会自然过渡到调用write工具写证明

### 问题2：Connection lost（连接中断）

**现象**：屏幕显示 `⚠︎ Connection lost, retrying...`

**影响**：thinking内容不持久化在对话历史中。connection lost时正在进行的API请求被中断，thinking内容**完全丢失**。

**处理方法**：
- 无法预防，这是API连接问题
- 重连后AI会重新思考，但推理深度可能降低
- 如果重连后thinking字符数远少于之前，考虑杀掉session重新开始

### 问题3：工具批准提示

**现象**：AI调用exec/write工具时，devin cli会弹出批准提示

**处理方法**：选3（always allow in this dir）——避免后续重复批准。
```bash
tmux send-keys -t <session-name> "3" Enter
```

对于write工具的批准提示，选2（accept edits mode）——后续所有文件写入自动批准。

### 问题4：pipe-pane日志被ANSI转义序列污染

**处理方法**：用perl清理后查看：
```bash
cat runs/<run_id>/tmux_pipe.log | perl -pe 's/\x1b\[[0-9;]*[a-zA-Z]//g' | perl -pe 's/[\x{2800}-\x{28ff}]//g' | grep -v '^$' | tail -60
```

注意：pipe-pane日志可能不完整——tmux的scrollback buffer有限。如果需要完整输出，用devin cli的`--export`参数。

## 判断Solver是否"做出来了"

1. **检查文件**：`ls -la ${EXP_DIR}/proof*` ——AI是否写了证明文件
2. **读证明**：`cat ${EXP_DIR}/proof.md` ——证明内容是否正确
3. **看对话状态**：AI是否说了"证毕"或"QED"
4. **看thinking字符数**：如果AI在第二问上thinking超过50k字符但没写文件，可能是"想了很多但做不出来"
5. **看工具调用**：AI是否调用了exec做数值验证——调用exec通常表示AI在认真尝试；不调用exec只在thinking里转，可能是卡住了

## 不要做的事

- **不要在AI思考时频繁发消息**——每次消息都会打断AI的thinking，丢失推理
- **不要发"continue"超过2次**——如果AI反复被截断，说明输出策略有问题，应该杀掉session重新开始
- **不要在AI工作期间修改工作目录的文件**——可能干扰AI的文件操作
