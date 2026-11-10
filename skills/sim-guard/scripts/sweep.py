# -*- coding: utf-8 -*-
"""
sweep.py —— 参数扫描和多阶段任务链，只依赖 Python 标准库。

  python sweep.py plan <sweep.json>                 展开算例，生成目录，不提交
  python sweep.py advance <sweep.json> [--wait 540] 推进一步或等待；可以反复运行
  python sweep.py status <sweep.json>               查看每个算例的状态
  python sweep.py retry <sweep.json> <算例 id>      用户确认后，重置停下的算例
  python sweep.py collect <sweep.json>              汇总结果并按判据判定，写 sweep_results.csv

advance 的退出码：0 = 全部完成，1 = 没有在跑的了，但有算例停下等人处理，3 = 还有任务未结束。
collect 的退出码：0 = 没发现问题，1 = 有判据被改过、结果文件坏了，或参数不同而结果完全相同。
状态在 sweep_state.json 里，每个算例处理完就保存。重复运行不会重复提交，已完成的算例不会重算；
plan、advance、retry 互斥，同一个扫描同时只能运行一个。
"""
import argparse
import contextlib
import csv
import fnmatch
import hashlib
import io
import itertools
import json
import os
import pathlib
import re
import shutil
import sys
import time

import sg


def _detect_lang():
    v = os.environ.get("SIMGUARD_LANG", "").strip().lower()
    if v in ("zh", "en", "both"):
        return v
    loc = ""
    try:
        import locale
        loc = (locale.getlocale()[0] or "")
    except Exception:
        pass
    if not loc and os.name == "nt":
        try:
            import ctypes
            return "zh" if (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x04 else "en"
        except Exception:
            pass
    return "zh" if loc.lower().startswith(("zh", "chinese")) else "en"


LANG = _detect_lang()


def T(zh, en):
    """Pick the message language: SIMGUARD_LANG=zh|en|both, default from the OS locale."""
    if LANG == "zh":
        return zh
    if LANG == "en":
        return en
    return f"{zh}\n{en}" if "\n" in zh else f"{zh} / {en}"


# 只包住已经翻译过的内容时用这些标点，避免 both 模式把整句重复两遍
COLON = "：" if LANG == "zh" else ": "
SEMI = "；" if LANG == "zh" else "; "
ERROR_PREFIX = {"zh": "错误：", "en": "error: "}.get(LANG, "错误 / error: ")


STATE_FILE = "sweep_state.json"
LOCK_FILE = ".sweep.lock"
RESULTS_FILE = "sweep_results.csv"
DEFAULT_MAX_CASES = 200
DEFAULT_MAX_RETRY = 1
POLL_S = 15
FINAL = ("done", "needs_attention")
JOB_STATUS = re.compile(r"\s(pending|running|done|failed|cancelled|timeout|lost)\s")
SKIP_NAMES = ("__pycache__", "*.class", ".simguard", "work")   # 复制模板和计算模板哈希都忽略这些
SKIP_COPY = shutil.ignore_patterns(*SKIP_NAMES)


@contextlib.contextmanager
def exclusive(base):
    """同一个扫描同时只能有一个 advance，否则会重复提交。"""
    f = open(pathlib.Path(base) / LOCK_FILE, "a+")
    try:
        try:
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise sg.UsageError(T("另一个 advance 正在推进这个扫描；等它结束再运行",
                               "another advance is already running on this sweep; wait for it to finish"))
    try:
        yield
    finally:
        f.close()


# ---------------------------------------------------------------- 展开和生成

def load_sweep(path):
    path = pathlib.Path(path).resolve()
    if not path.is_file():
        raise sg.UsageError(T(f"找不到 {path}", f"not found: {path}"))
    sweep = sg.read_json(path)
    errs = []
    for key in ("name", "line", "solver", "template", "params", "stages"):
        if not sweep.get(key):
            errs.append(T(f"缺少 {key}", f"missing {key}"))
    for stage in sweep.get("stages") or []:
        for key in ("name", "script", "cores", "mem_gb"):
            if not stage.get(key):
                errs.append(T(f"阶段缺少 {key}：{stage}", f"stage missing {key}: {stage}"))
        if not stage.get("outputs"):
            errs.append(T(f"阶段 {stage.get('name')} 要写 outputs：有些求解器失败时退出码也是 0",
                          f"stage {stage.get('name')} needs outputs: some solvers exit 0 even on failure"))
        if not stage.get("guarded") and not stage.get("max_h"):
            errs.append(T(f"阶段 {stage.get('name')} 不是 guarded，会作为短测试提交，要写 max_h（不超过 simq 的 smoke_max_h，默认 0.5）",
                          f"stage {stage.get('name')} is not guarded and will be submitted as a short test; "
                          f"set max_h (at most simq smoke_max_h, default 0.5)"))
    if not any(stage.get("guarded") for stage in sweep.get("stages") or []):
        errs.append(T("至少要有一个 guarded 阶段，正式求解要先通过 preflight",
                       "need at least one guarded stage: production solves must pass preflight"))
    if errs:
        raise sg.UsageError(T("sweep 文件不合格", "invalid sweep file") + COLON + SEMI.join(errs))
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
            raise sg.UsageError(T("mode=zip 时每个参数的取值个数要相同", "mode=zip needs the same number of values for every param"))
        rows = zip(*(params[n] for n in names))
    else:
        raise sg.UsageError(T("mode 只能是 grid 或 zip", "mode must be grid or zip"))
    cases = [dict(zip(names, row)) for row in rows]
    limit = sweep.get("max_cases", DEFAULT_MAX_CASES)
    if len(cases) > limit:
        raise sg.UsageError(T(f"展开后有 {len(cases)} 个算例，超过 max_cases={limit}；确认确实要跑这么多再调大",
                               f"{len(cases)} cases after expansion, over max_cases={limit}; raise it only if you really mean to run that many"))
    return cases


def case_id(params):
    text = json.dumps(params, sort_keys=True, ensure_ascii=False)
    return "c_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def skipped(rel):
    return any(fnmatch.fnmatch(part, pat) for part in rel.parts for pat in SKIP_NAMES)


def tree_hash(root):
    """只算会被复制进算例的文件：在模板里试跑留下的编译产物和 work 目录不算改动。"""
    root = pathlib.Path(root)
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file() and not skipped(p.relative_to(root))):
        h.update(f.relative_to(root).as_posix().encode("utf-8"))
        h.update(sg.sha256_file(f).encode("ascii"))
    return h.hexdigest()


def substitute(value, params):
    """把 manifest 里的 {"$param": "k"} 换成这个算例的取值，读回才能核对参数真的生效。"""
    if isinstance(value, dict):
        if set(value) == {"$param"}:
            if value["$param"] not in params:
                raise sg.UsageError(T(f"manifest 引用了不存在的参数：{value['$param']}",
                                       f"manifest refers to unknown param: {value['$param']}"))
            return params[value["$param"]]
        return {k: substitute(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, params) for v in value]
    return value


def make_case(base, sweep, template, params):
    cid = case_id(params)
    run = base / "cases" / cid
    if run.exists():            # 上次 plan 中途失败留下的半成品：状态里没有它，没有提交过任务
        shutil.rmtree(run)
    try:
        shutil.copytree(template, run, ignore=SKIP_COPY)
        manifest = substitute(sg.read_json(run / "manifest.json"), params)
        manifest.update(run_id=f"{sweep['name']}_{cid}", line=sweep["line"], solver=sweep["solver"], jobs=[])
        sg.write_json(run / "manifest.json", manifest)
        sg.write_json(run / "params.json", params)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = sg.cmd_freeze(argparse.Namespace(run_dir=str(run), amend=None))
        if code:
            raise sg.UsageError(T(f"{cid} 判据冻结失败", f"{cid}: gate freeze failed") + COLON + out.getvalue())
    except Exception:
        shutil.rmtree(run, ignore_errors=True)
        raise
    return cid


def cmd_plan(a):
    base, sweep = load_sweep(a.sweep)
    template = (base / sweep["template"]).resolve()
    for name in ("gate.json", "manifest.json"):
        if not (template / name).is_file():
            raise sg.UsageError(T(f"模板目录缺少 {name}", f"template dir is missing {name}"))
    errs = sg.gate_errors(sg.read_json(template / "gate.json"))
    if errs:
        return sg.fail_list(T("模板的判据不合格；判据要在展开算例前定好",
                               "template gate is invalid; fix the gate before expanding cases"), errs)
    cases = expand(sweep)
    digest = tree_hash(template)
    state_f = base / STATE_FILE
    state = sg.read_json(state_f) if state_f.is_file() else {"template_sha256": digest, "cases": {}}
    if state["template_sha256"] != digest:
        return sg.fail_list(T("模板已改过", "template has changed"),
                            [T("同一个扫描不能混用不同版本的模板；请用新的扫描目录",
                               "one sweep cannot mix template versions; use a new sweep directory")])
    added = 0
    sg.write_json(state_f, state)
    for params in cases:
        cid = case_id(params)
        if cid not in state["cases"]:
            make_case(base, sweep, template, params)
            state["cases"][cid] = {"params": params, "stages": {}}
            sg.write_json(state_f, state)      # 每生成一个就保存，中途失败也不丢已完成的
            added += 1
    stages = sweep["stages"]
    chain = ' → '.join(s['name'] for s in stages)
    print(T(f"算例 {len(cases)} 个，新增 {added} 个；阶段：{chain}",
            f"{len(cases)} cases, {added} new; stages: {chain}"))
    est = [s.get("est_h") for s in stages]
    if all(est):
        print(T(f"预计串行总时长：{sum(est) * len(cases):g} 小时", f"estimated serial total: {sum(est) * len(cases):g} h"))
    return 0


# ---------------------------------------------------------------- 推进

def job_state(simq, job_id):
    """只信 simq 输出里的状态。读不到（例如 job.json 正在写）就当作 unknown，下一轮再看，不判失败。"""
    done = sg.simq_call(simq, "wait", job_id, "--timeout", 0)
    match = JOB_STATUS.search(done.stdout)
    return match[1] if match else "unknown"


def outputs_fresh(run, stage, since):
    """产物要存在，并且是这次运行之后写的。"""
    missing = []
    for rel in stage["outputs"]:
        f = run / rel
        if not f.is_file() or f.stat().st_mtime < since - 2:
            missing.append(rel)
    return missing


def outputs_broken(run, stage):
    """JSON 产物要能读，而且没有 NaN 或无穷大。"""
    paths = {pathlib.Path(o).stem: o for o in stage["outputs"] if str(o).lower().endswith(".json")}
    sources, errs = sg.load_sources(run, paths)
    return errs + sg.nonfinite_errors(sources)


def supports_smoke(simq):
    return "--smoke" in sg.simq_call(simq, "submit", "--help").stdout


def submit_stage(simq, run, sweep, stage, rec):
    script = sg.resolve_script(run, stage["script"]).resolve()
    gate_sha = None
    if stage.get("guarded"):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            passed, gate_sha = sg.run_preflight(run, script, sweep["solver"])
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
    job = sg.parse_job_id(done.stdout)
    if not job:
        rec.update(status="needs_attention", cause="submit",
                   detail=[T(f"simq 没有返回 job id，先用 simq list 确认是否已提交：{done.stdout.strip()}",
                             f"simq returned no job id; check simq list for whether it was submitted: {done.stdout.strip()}")])
        return
    rec.update(status="active", job=job, submitted_ts=time.time(), attempts=rec.get("attempts", 0) + 1)
    rec.setdefault("jobs", []).append(job)
    if stage.get("guarded"):
        sg.record_job(run, job, gate_sha)
    else:
        sg.record_job(run, job, key="build_jobs")


def settle(simq, run, stage, rec, max_retry):
    """任务结束后判定这个阶段：done、重试，或停下等人。"""
    status = job_state(simq, rec["job"])
    if status in ("pending", "running", "unknown"):
        return
    if status == "done":
        missing = outputs_fresh(run, stage, rec["submitted_ts"])
        if missing:
            rec.update(status="needs_attention", cause="false_done", detail=[T(f"任务显示 done，但缺少这次的产物：{missing}",
                                                                            f"job shows done but outputs from this run are missing: {missing}")])
            return
        broken = outputs_broken(run, stage)
        if broken:
            rec.update(status="needs_attention", cause="false_done", detail=[T("任务显示 done，但产物有问题", "job shows done but outputs are bad")] + broken)
            return
        rec.update(status="done")
        return
    if status == "cancelled":
        rec.update(status="needs_attention", cause="cancelled", detail=[T("任务被取消", "job was cancelled")])
        return
    tail = sg.simq_call(simq, "tail", rec["job"], "-n", 200).stdout
    cause = "lost" if status == "lost" else sg.classify_failure(tail)[0]
    retryable = cause == "lost" or cause in sg.RETRYABLE
    if retryable and rec.get("attempts", 1) <= max_retry:
        rec.update(status="new", last_failure=cause)
        return
    rec.update(status="needs_attention", cause=cause, detail=[T(f"job {rec['job']} 状态 {status}", f"job {rec['job']} status {status}"),
                                                    sg.FAILURE_ADVICE.get(cause, T("读日志", "read the log"))])


def advance_case(simq, base, sweep, cid, case):
    run = base / "cases" / cid
    max_retry = sweep.get("max_retry", DEFAULT_MAX_RETRY)
    for stage in sweep["stages"]:
        rec = case["stages"].setdefault(stage["name"], {"status": "new"})
        try:
            if rec["status"] == "active":
                settle(simq, run, stage, rec, max_retry)
            if rec["status"] == "new":
                submit_stage(simq, run, sweep, stage, rec)
        except Exception as e:      # 一个算例出错只停这一个，不中断整轮
            if rec["status"] == "active":
                rec.setdefault("errors", []).append(f"{type(e).__name__}: {e}")   # 任务还在跑，下一轮再看
            else:
                rec.update(status="needs_attention", cause="error", detail=[f"{type(e).__name__}: {e}"])
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
        raise sg.UsageError(T("还没有展开算例：先跑 sweep.py plan", "no cases yet: run sweep.py plan first"))
    return sg.read_json(f)


def cmd_advance(a):
    base, sweep = load_sweep(a.sweep)
    simq = sg.find_simq(a, sg.load_project(base))
    state = load_state(base)
    deadline = time.time() + a.wait
    while True:
        for cid, case in state["cases"].items():
            advance_case(simq, base, sweep, cid, case)
            sg.write_json(base / STATE_FILE, state)    # 每个算例之后都保存：中途被杀也不会丢 job id、重复提交
        n = counts(state, sweep)
        if not n["active"] or time.time() >= deadline:
            break
        time.sleep(min(POLL_S, max(1, deadline - time.time())))
    print(T(f"完成 {n['done']}，进行中 {n['active']}，停下 {n['needs_attention']}",
            f"done {n['done']}, active {n['active']}, needs attention {n['needs_attention']}"))
    print_attention(state, sweep)
    if n["active"]:
        return 3
    return 1 if n["needs_attention"] else 0


def print_attention(state, sweep):
    for cid, case in state["cases"].items():
        kind, stage, rec = overall(case, sweep)
        if kind == "needs_attention":
            detail = ' '.join(rec.get('detail') or [])
            print(f"  {cid} {stage}{COLON}{rec.get('cause')} {detail}")


def cmd_status(a):
    base, sweep = load_sweep(a.sweep)
    state = load_state(base)
    for cid, case in state["cases"].items():
        kind, stage, rec = overall(case, sweep)
        print(f"{cid}  {kind:<16} {stage:<10} {rec.get('job', ''):<30} {json.dumps(case['params'], ensure_ascii=False)}")
    n = counts(state, sweep)
    print(T(f"完成 {n['done']}，进行中 {n['active']}，停下 {n['needs_attention']}",
            f"done {n['done']}, active {n['active']}, needs attention {n['needs_attention']}"))
    return 0


def cmd_retry(a):
    base, sweep = load_sweep(a.sweep)
    state = load_state(base)
    case = state["cases"].get(a.case)
    if not case:
        raise sg.UsageError(T(f"找不到算例 {a.case}", f"case not found: {a.case}"))
    reset = [n for n, rec in case["stages"].items() if rec["status"] == "needs_attention"]
    if not reset:
        print(T("这个算例没有停下的阶段", "this case has no stopped stage"))
        return 0
    for name in reset:
        rec = case["stages"][name]
        rec.update(status="new", attempts=0)
        for key in ("cause", "detail"):
            rec.pop(key, None)
    sg.write_json(base / STATE_FILE, state)
    print(T(f"已重置 {a.case} 的 {reset}；运行 advance 继续", f"reset {reset} of {a.case}; run advance to continue"))
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


def collect_case(run, kind):
    """返回 (结果, 判据结论, 问题)。判据被改过或结果坏了，结论记 BLOCKED，不拿新阈值判。"""
    problems = []
    criteria = sg.read_json(run / "gate.json").get("criteria", [])
    if kind != "done":
        return {}, {c["id"]: "NOT_RUN" for c in criteria}, problems
    lock_errs, gate_sha = sg.gate_lock_errors(run)
    problems += lock_errs + (sg.submitted_gate_errors(run, gate_sha) if gate_sha else [])
    result = {}
    result_f = run / "result.json"
    if result_f.is_file():
        try:
            result = sg.read_json(result_f)
            if not isinstance(result, dict):
                raise sg.UsageError(T("result.json 要是一个对象", "result.json must be an object"))
        except sg.UsageError as e:
            problems.append(str(e))
            result = {}
    problems += sg.result_errors(run, sg.read_json(run / "manifest.json"))
    if lock_errs or (problems and not result):
        return result, {c["id"]: "BLOCKED" for c in criteria}, problems
    return result, judge(criteria, result), problems


def degenerate_keys(rows, params):
    """参数取值不同，结果却完全相同：常见于参数没有真的传进模型（A2）。"""
    done = [r for r in rows if r["state"] == "done"]
    if len(done) < 2 or len({json.dumps([r.get(f"param:{p}") for p in params]) for r in done}) < 2:
        return []
    keys = {k for r in done for k, v in r.items() if isinstance(v, (int, float)) and not k.startswith(("param:", "gate:"))}
    return sorted(k for k in keys if len({r.get(k) for r in done}) == 1)


def cmd_collect(a):
    base, sweep = load_sweep(a.sweep)
    state = load_state(base)
    rows, issues = [], []
    for cid, case in state["cases"].items():
        run = base / "cases" / cid
        kind = overall(case, sweep)[0]
        try:
            result, verdicts, problems = collect_case(run, kind)
        except sg.UsageError as e:
            result, verdicts, problems = {}, {}, [str(e)]
        issues += [f"{cid}{COLON}{p}" for p in problems]
        row = {"case": cid, "state": kind}
        row.update({f"param:{k}": v for k, v in case["params"].items()})
        row.update({k: v for k, v in result.items() if isinstance(v, (int, float, str))})
        row.update({f"gate:{k}": v for k, v in verdicts.items()})
        row["problems"] = SEMI.join(problems)
        rows.append(row)
    fields = []
    for row in rows:
        fields += [k for k in row if k not in fields]
    with open(base / RESULTS_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    gates = [v for row in rows for k, v in row.items() if k.startswith("gate:")]
    print(T(f"已写 {base / RESULTS_FILE}：{len(rows)} 个算例，判据 PASS {gates.count('PASS')}，FAIL {gates.count('FAIL')}，"
            f"未运行 {gates.count('NOT_RUN')}，需人工 {gates.count('MANUAL')}，无法判定 {gates.count('BLOCKED')}",
            f"wrote {base / RESULTS_FILE}: {len(rows)} cases, gate PASS {gates.count('PASS')}, FAIL {gates.count('FAIL')}, "
            f"NOT_RUN {gates.count('NOT_RUN')}, MANUAL {gates.count('MANUAL')}, BLOCKED {gates.count('BLOCKED')}"))
    flat = degenerate_keys(rows, list(sweep["params"]))
    if flat:
        issues.append(T(f"参数不同但结果完全相同：{flat}。先确认参数真的传进了模型，再用这些结果",
                         f"different params gave identical results: {flat}. Check the params really reach the model before using these results"))
    for i in issues:
        print(f"  - {i}")
    return 1 if issues else 0


def main():
    ap = argparse.ArgumentParser(prog="sweep", description=T("参数扫描和多阶段任务链", "parameter sweeps and multi-stage job chains"))
    sub = ap.add_subparsers(dest="command", required=True)
    for name, fn in (("plan", cmd_plan), ("status", cmd_status), ("collect", cmd_collect)):
        s = sub.add_parser(name)
        s.add_argument("sweep")
        s.set_defaults(fn=fn)
    s = sub.add_parser("advance")
    s.add_argument("sweep")
    s.add_argument("--wait", type=float, default=0, help=T("最多等待这么多秒；工具超时前要返回，建议不超过 540",
                                                                  "wait at most this many seconds; return before the tool times out, 540 or less recommended"))
    s.add_argument("--simq")
    s.set_defaults(fn=cmd_advance)
    s = sub.add_parser("retry")
    s.add_argument("sweep")
    s.add_argument("case")
    s.set_defaults(fn=cmd_retry)
    a = ap.parse_args()
    try:
        if a.command in ("plan", "advance", "retry"):     # 会改状态的命令互斥
            with exclusive(pathlib.Path(a.sweep).resolve().parent):
                return a.fn(a)
        return a.fn(a)
    except sg.UsageError as e:
        print(f"{ERROR_PREFIX}{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
