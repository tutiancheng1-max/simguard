# -*- coding: utf-8 -*-
"""
sweep.py —— 参数扫描和多阶段任务链，只依赖 Python 标准库。

  python sweep.py plan <sweep.json>                 展开算例，生成目录，不提交
  python sweep.py advance <sweep.json> [--wait 540] 推进一步或等待；可以反复运行
  python sweep.py status <sweep.json>               查看每个算例的状态
  python sweep.py retry <sweep.json> <算例 id>      用户确认后，重置停下的算例
  python sweep.py collect <sweep.json>              汇总结果并按判据判定，写 sweep_results.csv

advance 的退出码：0 = 全部完成，1 = 没有在跑的了，但有算例停下等人处理，3 = 还有任务未结束。
状态在 sweep_state.json 里。重复运行不会重复提交，已完成的算例不会重算。
"""
import argparse
import contextlib
import csv
import hashlib
import io
import itertools
import json
import pathlib
import re
import shutil
import sys
import time

import sg

STATE_FILE = "sweep_state.json"
RESULTS_FILE = "sweep_results.csv"
DEFAULT_MAX_CASES = 200
DEFAULT_MAX_RETRY = 1
POLL_S = 15
FINAL = ("done", "needs_attention")
JOB_STATUS = re.compile(r"\s(pending|running|done|failed|cancelled|timeout|lost)\s")
SKIP_COPY = shutil.ignore_patterns("__pycache__", "*.class", ".simguard", "work")


# ---------------------------------------------------------------- 展开和生成

def load_sweep(path):
    path = pathlib.Path(path).resolve()
    if not path.is_file():
        raise sg.UsageError(f"找不到 {path}")
    sweep = sg.read_json(path)
    errs = []
    for key in ("name", "line", "solver", "template", "params", "stages"):
        if not sweep.get(key):
            errs.append(f"缺少 {key}")
    for stage in sweep.get("stages") or []:
        for key in ("name", "script", "cores", "mem_gb"):
            if not stage.get(key):
                errs.append(f"阶段缺少 {key}：{stage}")
        if not stage.get("outputs"):
            errs.append(f"阶段 {stage.get('name')} 要写 outputs：有些求解器失败时退出码也是 0")
    if not any(stage.get("guarded") for stage in sweep.get("stages") or []):
        errs.append("至少要有一个 guarded 阶段，正式求解要先通过 preflight")
    if errs:
        raise sg.UsageError("sweep 文件不合格：" + "；".join(errs))
    return path.parent, sweep


def expand(sweep):
    params = sweep["params"]
    names = list(params)
    mode = sweep.get("mode", "grid")
    if mode == "grid":
        rows = itertools.product(*(params[n] for n in names))
    elif mode == "zip":
        sizes = {len(params[n]) for n in names}
        if len(sizes) != 1:
            raise sg.UsageError("mode=zip 时每个参数的取值个数要相同")
        rows = zip(*(params[n] for n in names))
    else:
        raise sg.UsageError("mode 只能是 grid 或 zip")
    cases = [dict(zip(names, row)) for row in rows]
    limit = sweep.get("max_cases", DEFAULT_MAX_CASES)
    if len(cases) > limit:
        raise sg.UsageError(f"展开后有 {len(cases)} 个算例，超过 max_cases={limit}；确认确实要跑这么多再调大")
    return cases


def case_id(params):
    text = json.dumps(params, sort_keys=True, ensure_ascii=False)
    return "c_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def tree_hash(root):
    h = hashlib.sha256()
    for f in sorted(p for p in pathlib.Path(root).rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(str(f.relative_to(root)).encode("utf-8"))
        h.update(sg.sha256_file(f).encode("ascii"))
    return h.hexdigest()


def substitute(value, params):
    """把 manifest 里的 {"$param": "k"} 换成这个算例的取值，读回才能核对参数真的生效。"""
    if isinstance(value, dict):
        if set(value) == {"$param"}:
            if value["$param"] not in params:
                raise sg.UsageError(f"manifest 引用了不存在的参数：{value['$param']}")
            return params[value["$param"]]
        return {k: substitute(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, params) for v in value]
    return value


def make_case(base, sweep, template, params):
    cid = case_id(params)
    run = base / "cases" / cid
    shutil.copytree(template, run, ignore=SKIP_COPY)
    manifest = substitute(sg.read_json(run / "manifest.json"), params)
    manifest.update(run_id=f"{sweep['name']}_{cid}", line=sweep["line"], solver=sweep["solver"], jobs=[])
    sg.write_json(run / "manifest.json", manifest)
    sg.write_json(run / "params.json", params)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        code = sg.cmd_freeze(argparse.Namespace(run_dir=str(run), amend=None))
    if code:
        raise sg.UsageError(f"{cid} 判据冻结失败：{out.getvalue()}")
    return cid


def cmd_plan(a):
    base, sweep = load_sweep(a.sweep)
    template = (base / sweep["template"]).resolve()
    for name in ("gate.json", "manifest.json"):
        if not (template / name).is_file():
            raise sg.UsageError(f"模板目录缺少 {name}")
    errs = sg.gate_errors(sg.read_json(template / "gate.json"))
    if errs:
        return sg.fail_list("模板的判据不合格；判据要在展开算例前定好", errs)
    cases = expand(sweep)
    digest = tree_hash(template)
    state_f = base / STATE_FILE
    state = sg.read_json(state_f) if state_f.is_file() else {"template_sha256": digest, "cases": {}}
    if state["template_sha256"] != digest:
        return sg.fail_list("模板已改过", ["同一个扫描不能混用不同版本的模板；请用新的扫描目录"])
    added = 0
    for params in cases:
        cid = case_id(params)
        if cid not in state["cases"]:
            make_case(base, sweep, template, params)
            state["cases"][cid] = {"params": params, "stages": {}}
            added += 1
    sg.write_json(state_f, state)
    stages = sweep["stages"]
    print(f"算例 {len(cases)} 个，新增 {added} 个；阶段：{' → '.join(s['name'] for s in stages)}")
    est = [s.get("est_h") for s in stages]
    if all(est):
        print(f"预计串行总时长：{sum(est) * len(cases):g} 小时")
    return 0


# ---------------------------------------------------------------- 推进

def job_state(simq, job_id):
    done = sg.simq_call(simq, "wait", job_id, "--timeout", 0)
    match = JOB_STATUS.search(done.stdout)
    if match:
        return match[1]
    return "failed" if done.returncode == 1 else "running"


def outputs_fresh(run, stage, since):
    """产物要存在，并且是这次运行之后写的。"""
    missing = []
    for rel in stage["outputs"]:
        f = run / rel
        if not f.is_file() or f.stat().st_mtime < since - 2:
            missing.append(rel)
    return missing


def supports_smoke(simq):
    return "--smoke" in sg.simq_call(simq, "submit", "--help").stdout


def submit_stage(simq, run, sweep, stage, rec):
    script = sg.resolve_script(run, stage["script"]).resolve()
    if stage.get("guarded"):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            passed = sg.run_preflight(run, script, sweep["solver"])
        if not passed:
            rec.update(status="needs_attention", cause="preflight",
                       detail=[x.strip() for x in out.getvalue().splitlines() if x.strip().startswith("- ")])
            return
    args = ["submit", "--line", sweep["line"], "--name", f"{run.name}-{stage['name']}", "--sw", sweep["solver"],
            "--cores", stage["cores"], "--mem", stage["mem_gb"], "--cwd", run, "--script", script]
    if stage.get("est_h"):
        args += ["--est", stage["est_h"]]
    if stage.get("max_h"):
        args += ["--max-h", stage["max_h"]]
    if not stage.get("guarded") and supports_smoke(simq):
        args.append("--smoke")
    done = sg.simq_call(simq, *args)
    if done.returncode:
        rec.update(status="needs_attention", cause="submit", detail=[done.stderr.strip()])
        return
    job = done.stdout.strip().splitlines()[-1]
    rec.update(status="active", job=job, submitted_ts=time.time(), attempts=rec.get("attempts", 0) + 1)
    rec.setdefault("jobs", []).append(job)
    if stage.get("guarded"):
        manifest = sg.read_json(run / "manifest.json")
        manifest["jobs"] = (manifest.get("jobs") or []) + [job]
        sg.write_json(run / "manifest.json", manifest)


def settle(simq, run, stage, rec, max_retry):
    """任务结束后判定这个阶段：done、重试，或停下等人。"""
    status = job_state(simq, rec["job"])
    if status in ("pending", "running"):
        return
    if status == "done":
        missing = outputs_fresh(run, stage, rec["submitted_ts"])
        if not missing:
            rec.update(status="done")
            return
        rec.update(status="needs_attention", cause="false_done", detail=[f"任务显示 done，但缺少这次的产物：{missing}"])
        return
    if status == "cancelled":
        rec.update(status="needs_attention", cause="cancelled", detail=["任务被取消"])
        return
    tail = sg.simq_call(simq, "tail", rec["job"], "-n", 200).stdout
    cause = "lost" if status == "lost" else sg.classify_failure(tail)[0]
    retryable = cause == "lost" or cause in sg.RETRYABLE
    if retryable and rec.get("attempts", 1) <= max_retry:
        rec.update(status="new", last_failure=cause)
        return
    rec.update(status="needs_attention", cause=cause, detail=[f"job {rec['job']} 状态 {status}", sg.FAILURE_ADVICE.get(cause, "读日志")])


def advance_case(simq, base, sweep, cid, case):
    run = base / "cases" / cid
    max_retry = sweep.get("max_retry", DEFAULT_MAX_RETRY)
    for stage in sweep["stages"]:
        rec = case["stages"].setdefault(stage["name"], {"status": "new"})
        if rec["status"] == "active":
            settle(simq, run, stage, rec, max_retry)
        if rec["status"] == "new":
            submit_stage(simq, run, sweep, stage, rec)
        if rec["status"] != "done":
            return


def overall(case, sweep):
    for stage in sweep["stages"]:
        rec = case["stages"].get(stage["name"], {"status": "new"})
        if rec["status"] == "done":
            continue
        if rec["status"] == "needs_attention":
            return "needs_attention", stage["name"], rec
        return "active", stage["name"], rec
    return "done", "", {}


def counts(state, sweep):
    out = {"done": 0, "active": 0, "needs_attention": 0}
    for case in state["cases"].values():
        out[overall(case, sweep)[0]] += 1
    return out


def load_state(base):
    f = base / STATE_FILE
    if not f.is_file():
        raise sg.UsageError("还没有展开算例：先跑 sweep.py plan")
    return sg.read_json(f)


def cmd_advance(a):
    base, sweep = load_sweep(a.sweep)
    simq = sg.find_simq(a, sg.load_project(base))
    state = load_state(base)
    deadline = time.time() + a.wait
    while True:
        for cid, case in state["cases"].items():
            advance_case(simq, base, sweep, cid, case)
        sg.write_json(base / STATE_FILE, state)
        n = counts(state, sweep)
        if not n["active"] or time.time() >= deadline:
            break
        time.sleep(min(POLL_S, max(1, deadline - time.time())))
    print(f"完成 {n['done']}，进行中 {n['active']}，停下 {n['needs_attention']}")
    print_attention(state, sweep)
    if n["active"]:
        return 3
    return 1 if n["needs_attention"] else 0


def print_attention(state, sweep):
    for cid, case in state["cases"].items():
        kind, stage, rec = overall(case, sweep)
        if kind == "needs_attention":
            print(f"  {cid} {stage}：{rec.get('cause')} {' '.join(rec.get('detail') or [])}")


def cmd_status(a):
    base, sweep = load_sweep(a.sweep)
    state = load_state(base)
    for cid, case in state["cases"].items():
        kind, stage, rec = overall(case, sweep)
        print(f"{cid}  {kind:<16} {stage:<10} {rec.get('job', ''):<30} {json.dumps(case['params'], ensure_ascii=False)}")
    n = counts(state, sweep)
    print(f"完成 {n['done']}，进行中 {n['active']}，停下 {n['needs_attention']}")
    return 0


def cmd_retry(a):
    base, sweep = load_sweep(a.sweep)
    state = load_state(base)
    case = state["cases"].get(a.case)
    if not case:
        raise sg.UsageError(f"找不到算例 {a.case}")
    reset = [n for n, rec in case["stages"].items() if rec["status"] == "needs_attention"]
    if not reset:
        print("这个算例没有停下的阶段")
        return 0
    for name in reset:
        rec = case["stages"][name]
        rec.update(status="new", attempts=0)
        for key in ("cause", "detail"):
            rec.pop(key, None)
    sg.write_json(base / STATE_FILE, state)
    print(f"已重置 {a.case} 的 {reset}；运行 advance 继续")
    return 0


# ---------------------------------------------------------------- 汇总

OPS = {"<": lambda x, t: x < t, "<=": lambda x, t: x <= t, ">": lambda x, t: x > t,
       ">=": lambda x, t: x >= t, "==": lambda x, t: x == t}


def judge(criteria, result):
    """按冻结的判据判定。写了 result_key 的判据才能自动判；缺值判 FAIL。"""
    verdicts = {}
    for c in criteria:
        key = c.get("result_key")
        if not key or c.get("op") not in OPS:
            verdicts[c["id"]] = "MANUAL"
        elif not isinstance(result.get(key), (int, float)):
            verdicts[c["id"]] = "FAIL"
        else:
            verdicts[c["id"]] = "PASS" if OPS[c["op"]](result[key], c["threshold"]) else "FAIL"
    return verdicts


def cmd_collect(a):
    base, sweep = load_sweep(a.sweep)
    state = load_state(base)
    rows = []
    for cid, case in state["cases"].items():
        run = base / "cases" / cid
        kind = overall(case, sweep)[0]
        result_f = run / "result.json"
        result = sg.read_json(result_f) if kind == "done" and result_f.is_file() else {}
        criteria = sg.read_json(run / "gate.json").get("criteria", [])
        verdicts = judge(criteria, result) if kind == "done" else {c["id"]: "NOT_RUN" for c in criteria}
        row = {"case": cid, "state": kind}
        row.update({f"param:{k}": v for k, v in case["params"].items()})
        row.update({k: v for k, v in result.items() if isinstance(v, (int, float, str))})
        row.update({f"gate:{k}": v for k, v in verdicts.items()})
        rows.append(row)
    fields = []
    for row in rows:
        fields += [k for k in row if k not in fields]
    with open(base / RESULTS_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    gates = [v for row in rows for k, v in row.items() if k.startswith("gate:")]
    print(f"已写 {base / RESULTS_FILE}：{len(rows)} 个算例，判据 PASS {gates.count('PASS')}，FAIL {gates.count('FAIL')}，"
          f"未运行 {gates.count('NOT_RUN')}，需人工 {gates.count('MANUAL')}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="sweep", description="参数扫描和多阶段任务链")
    sub = ap.add_subparsers(dest="command", required=True)
    for name, fn in (("plan", cmd_plan), ("status", cmd_status), ("collect", cmd_collect)):
        s = sub.add_parser(name)
        s.add_argument("sweep")
        s.set_defaults(fn=fn)
    s = sub.add_parser("advance")
    s.add_argument("sweep")
    s.add_argument("--wait", type=float, default=0, help="最多等待这么多秒；工具超时前要返回，建议不超过 540")
    s.add_argument("--simq")
    s.set_defaults(fn=cmd_advance)
    s = sub.add_parser("retry")
    s.add_argument("sweep")
    s.add_argument("case")
    s.set_defaults(fn=cmd_retry)
    a = ap.parse_args()
    try:
        return a.fn(a)
    except sg.UsageError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
