#!/usr/bin/env python3
"""test_devin_stdout_json.py — 临时测试脚本

测试目的：验证devin -p非交互模式下，AI的JSON输出是否直接到stdout，
脚本能否直接从stdout捕获JSON（不需要从export的conversation.json解析）。

测试结果：成功。三个场景都验证了stdout直接输出JSON：
1. 简单回显JSON → stdout直接拿到
2. 验证通过场景 → stdout直接拿到 {"question-right": true, "answer-right": true, ...}
3. 验证不通过场景 → stdout直接拿到 {"question-right": false, "answer-detail": "The extracted question drops...", ...}

结论：阶段3可以从stdout直接获取JSON，不需要从export解析。
export仍然保存用于审计追溯。

使用时间：2026-08-21
测试结果记录在：dev-docs/404-v0-2026-08-21-题目清洗系统方案.md §2.4

用法:
  python3 tmp-scripts/test_devin_stdout_json.py
"""
import subprocess
import json

MODEL = "glm-5.2-high"
COMMON_ARGS = ["--model", MODEL, "--respect-workspace-trust", "false", "--permission-mode", "dangerous"]

def run_devin_p(prompt):
    """运行devin -p，返回stdout"""
    result = subprocess.run(
        ["devin", "-p", prompt] + COMMON_ARGS,
        capture_output=True, text=True, timeout=120
    )
    return result.stdout.strip()

# 测试1：简单回显JSON
print("=== 测试1：简单回显JSON ===")
prompt1 = 'Output this exact JSON and nothing else: {"question-right": true, "answer-right": false, "question-detail": "", "answer-detail": "test"}'
stdout1 = run_devin_p(prompt1)
print(f"stdout: {stdout1}")
try:
    j1 = json.loads(stdout1)
    print(f"解析成功: {j1}")
except:
    print("解析失败")
print()

# 测试2：验证通过场景
print("=== 测试2：验证通过场景 ===")
prompt2 = '''You are a verification assistant. Compare the original text and extracted result, then output a JSON.

Original text: "What is 2+2? The answer is 4."
Extracted question: "What is 2+2?"
Extracted answer: "4"

Output ONLY this JSON format (no other text):
{"question-right": true/false, "answer-right": true/false, "question-detail": "reason if wrong, empty if right", "answer-detail": "reason if wrong, empty if right"}'''
stdout2 = run_devin_p(prompt2)
print(f"stdout: {stdout2}")
try:
    j2 = json.loads(stdout2)
    print(f"解析成功: {j2}")
except:
    print("解析失败")
print()

# 测试3：验证不通过场景
print("=== 测试3：验证不通过场景 ===")
prompt3 = '''You are a verification assistant. Compare the original text and extracted result, then output a JSON.

Original text: "Determine whether the cardinal characteristic (mathfrak{ridiculous}) is equal to (mathfrak{p}), where (mathfrak{ridiculous}) is defined as the minimal cardinality of a centered family of subsets of (mathbb{N}). The answer is (mathfrak{p})."
Extracted question: "Determine whether the cardinal characteristic is equal to p."
Extracted answer: "p"

Output ONLY this JSON format (no other text):
{"question-right": true/false, "answer-right": true/false, "question-detail": "reason if wrong, empty if right", "answer-detail": "reason if wrong, empty if right"}'''
stdout3 = run_devin_p(prompt3)
print(f"stdout: {stdout3}")
try:
    j3 = json.loads(stdout3)
    print(f"解析成功: {j3}")
    print(f"  question-right: {j3.get('question-right')}")
    print(f"  answer-right: {j3.get('answer-right')}")
    print(f"  question-detail: {j3.get('question-detail', '')[:100]}")
except:
    print("解析失败")
