#!/usr/bin/env python3
"""investigate_missing_files.py — 排查其他status的文件缺失原因

排查406报告中未排查的两个缺失项：
1. failed_no_proof的54题无文件（10无目录/54无export/54无pane/43无tmux）
2. candidate_solved的23题文件缺失（2无export/21无pane）

对每个缺失题：
- 查DB中run记录的详细信息（runtime/ended_at/started_at/end_reason）
- 查硬盘目录结构（列出所有文件和大小）
- 读pane_snapshot/tmux_pipe.log内容找线索
- 查该题是否有多次run（可能前一次run有文件）

用法:
  python investigate_missing_files.py
"""
import sys
import os
import json
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arango import ArangoClient

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
TRAJ_DIR = "/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory"


def list_files(exp_id):
    """列出trajectory目录的所有文件和大小"""
    path = os.path.join(TRAJ_DIR, exp_id)
    if not os.path.exists(path):
        return None, []
    files = []
    for root, dirs, fnames in os.walk(path):
        for f in fnames:
            rel = os.path.relpath(os.path.join(root, f), path)
            size = os.path.getsize(os.path.join(root, f))
            files.append((rel, size))
    return path, files


def read_file(exp_id, rel_path):
    path = os.path.join(TRAJ_DIR, exp_id, rel_path)
    if os.path.exists(path):
        try:
            with open(path, errors="replace") as f:
                return f.read()
        except Exception:
            return None
    return None


def main():
    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    # 查两组题的最后run
    print("查询failed_no_proof和candidate_solved的最后run...")
    aql = """
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  LET last_run = (
    FOR r IN devin_problem_runs
      FILTER r.problem_id == p._key
      FILTER r.batch_id == "pipe-runner"
      FILTER r.status != "running"
      SORT r.ended_at DESC
      LIMIT 1
      RETURN {exp_id: r.exp_id, status: r.status, ended_at: r.ended_at, started_at: r.started_at, runtime: r.runtime_seconds, end_reason: r.end_reason}
  )
  FILTER LENGTH(last_run) > 0
  FILTER last_run[0].status IN ["failed_no_proof", "candidate_solved"]
  RETURN {
    problem_id: p._key,
    source_dataset: p.source_dataset,
    exp_id: last_run[0].exp_id,
    status: last_run[0].status,
    ended_at: last_run[0].ended_at,
    started_at: last_run[0].started_at,
    runtime: last_run[0].runtime,
    end_reason: last_run[0].end_reason
  }
"""
    cursor = db.aql.execute(aql, ttl=300, batch_size=2000)
    all_runs = list(cursor)

    # 分组
    failed_no_proof = [r for r in all_runs if r["status"] == "failed_no_proof"]
    candidate_solved = [r for r in all_runs if r["status"] == "candidate_solved"]

    print(f"  failed_no_proof: {len(failed_no_proof)}")
    print(f"  candidate_solved: {len(candidate_solved)}")
    print()

    # 检查文件缺失
    EXEMPT_EXPORT = {"rate_limited", "failed_stall"}
    EXEMPT_PANE = {"failed_stall"}

    def check_files(run):
        exp_id = run["exp_id"]
        status = run["status"]
        path, files = list_files(exp_id)
        if path is None:
            return {"no_dir": True, "files": [], "has_si": False, "has_export": False,
                    "has_pane": False, "has_tmux": False}

        has_si = any(f[0] == "session_info.json" for f in files)
        has_export = any(f[0].startswith("exports/") for f in files)
        has_pane = any(f[0].startswith("collector/") for f in files)
        has_tmux = any(f[0].startswith("tmux/") for f in files)

        # 检查是否真的缺失（考虑豁免）
        export_missing = not has_export and status not in EXEMPT_EXPORT
        pane_missing = not has_pane and status not in EXEMPT_PANE

        return {
            "no_dir": False, "files": files,
            "has_si": has_si, "has_export": has_export,
            "has_pane": has_pane, "has_tmux": has_tmux,
            "export_missing": export_missing, "pane_missing": pane_missing,
        }

    # === 排查failed_no_proof ===
    print(f"=== 排查failed_no_proof（{len(failed_no_proof)}题）===")
    print()

    no_dir_list = []
    no_export_list = []
    no_pane_list = []
    no_tmux_list = []
    complete_list = []

    for run in failed_no_proof:
        info = check_files(run)
        if info["no_dir"]:
            no_dir_list.append((run, info))
        if not info.get("has_export", False):
            no_export_list.append((run, info))
        if not info.get("has_pane", False):
            no_pane_list.append((run, info))
        if not info.get("has_tmux", False):
            no_tmux_list.append((run, info))
        if info.get("has_si") and info.get("has_export") and info.get("has_pane") and info.get("has_tmux"):
            complete_list.append(run)

    print(f"  文件完整: {len(complete_list)}")
    print(f"  无目录: {len(no_dir_list)}")
    print(f"  无export: {len(no_export_list)}")
    print(f"  无pane: {len(no_pane_list)}")
    print(f"  无tmux: {len(no_tmux_list)}")
    print()

    # 详细排查每个缺失的题
    print(f"  --- 无目录的{len(no_dir_list)}题详情 ---")
    for run, info in no_dir_list[:10]:
        print(f"    {run['problem_id']} exp_id={run['exp_id']} runtime={run['runtime']}s ended={run['ended_at']}")
        print(f"      end_reason: {run['end_reason']}")
        # 查该题所有run
        aql2 = f"""
FOR r IN devin_problem_runs
  FILTER r.problem_id == "{run['problem_id']}"
  FILTER r.batch_id == "pipe-runner"
  SORT r.started_at DESC
  RETURN {{exp_id: r.exp_id, status: r.status, started_at: r.started_at, ended_at: r.ended_at, runtime: r.runtime_seconds}}
"""
        all_runs_for_problem = list(db.aql.execute(aql2, ttl=300))
        print(f"      所有run（{len(all_runs_for_problem)}次）:")
        for r in all_runs_for_problem[:5]:
            print(f"        {r['exp_id']} status={r['status']} started={r['started_at']} runtime={r['runtime']}s")
    print()

    print(f"  --- 无export的题（抽样10个，排除无目录的）---")
    no_export_with_dir = [(r, i) for r, i in no_export_list if not i["no_dir"]]
    for run, info in no_export_with_dir[:10]:
        print(f"    {run['problem_id']} exp_id={run['exp_id']} runtime={run['runtime']}s ended={run['ended_at']}")
        print(f"      end_reason: {run['end_reason']}")
        print(f"      文件列表:")
        for f, size in info["files"]:
            print(f"        {f}: {size}B")
        # 读tmux_pipe.log最后200字符
        tmux_content = read_file(run["exp_id"], "tmux/tmux_pipe.log")
        if tmux_content:
            print(f"      tmux_pipe.log最后200字符: {tmux_content[-200:]!r}")
        # 读session_info
        si = read_file(run["exp_id"], "session_info.json")
        if si:
            try:
                si_data = json.loads(si)
                print(f"      session_info: start={si_data.get('start_timestamp', '?')[:19]} interactive={si_data.get('interactive')}")
            except Exception:
                pass
    print()

    # === 排查candidate_solved ===
    print(f"=== 排查candidate_solved（{len(candidate_solved)}题）===")
    print()

    cs_no_export = []
    cs_no_pane = []
    cs_no_dir = []
    cs_complete = 0

    for run in candidate_solved:
        info = check_files(run)
        if info["no_dir"]:
            cs_no_dir.append((run, info))
        elif info.get("export_missing"):
            cs_no_export.append((run, info))
        elif info.get("pane_missing"):
            cs_no_pane.append((run, info))
        else:
            cs_complete += 1

    print(f"  文件完整: {cs_complete}")
    print(f"  无目录: {len(cs_no_dir)}")
    print(f"  无export: {len(cs_no_export)}")
    print(f"  无pane: {len(cs_no_pane)}")
    print()

    print(f"  --- candidate_solved无export的{len(cs_no_export)}题详情 ---")
    for run, info in cs_no_export:
        print(f"    {run['problem_id']} exp_id={run['exp_id']} runtime={run['runtime']}s ended={run['ended_at']}")
        print(f"      end_reason: {run['end_reason']}")
        print(f"      文件列表:")
        for f, size in info["files"]:
            print(f"        {f}: {size}B")
        # 查所有run
        aql3 = f"""
FOR r IN devin_problem_runs
  FILTER r.problem_id == "{run['problem_id']}"
  FILTER r.batch_id == "pipe-runner"
  SORT r.started_at DESC
  RETURN {{exp_id: r.exp_id, status: r.status, started_at: r.started_at, runtime: r.runtime_seconds}}
"""
        all_runs_for_problem = list(db.aql.execute(aql3, ttl=300))
        print(f"      所有run（{len(all_runs_for_problem)}次）:")
        for r in all_runs_for_problem[:5]:
            print(f"        {r['exp_id']} status={r['status']} started={r['started_at']} runtime={r['runtime']}s")
    print()

    print(f"  --- candidate_solved无pane的{len(cs_no_pane)}题详情（抽样10个）---")
    for run, info in cs_no_pane[:10]:
        print(f"    {run['problem_id']} exp_id={run['exp_id']} runtime={run['runtime']}s ended={run['ended_at']}")
        print(f"      end_reason: {run['end_reason']}")
        print(f"      文件列表:")
        for f, size in info["files"]:
            print(f"        {f}: {size}B")
        # 查所有run
        aql4 = f"""
FOR r IN devin_problem_runs
  FILTER r.problem_id == "{run['problem_id']}"
  FILTER r.batch_id == "pipe-runner"
  SORT r.started_at DESC
  RETURN {{exp_id: r.exp_id, status: r.status, started_at: r.started_at, runtime: r.runtime_seconds}}
"""
        all_runs_for_problem = list(db.aql.execute(aql4, ttl=300))
        if len(all_runs_for_problem) > 1:
            print(f"      所有run（{len(all_runs_for_problem)}次）:")
            for r in all_runs_for_problem[:5]:
                print(f"        {r['exp_id']} status={r['status']} started={r['started_at']} runtime={r['runtime']}s")
    print()

    # === 总结 ===
    print(f"=== 总结 ===")
    print(f"  failed_no_proof: {len(failed_no_proof)}题，{len(complete_list)}文件完整，{len(no_dir_list)}无目录，{len(no_export_list)}无export")
    print(f"  candidate_solved: {len(candidate_solved)}题，{cs_complete}文件完整，{len(cs_no_export)}无export，{len(cs_no_pane)}无pane")


if __name__ == "__main__":
    main()
