# -*- coding: utf-8 -*-
"""
sg.py —— sim-guard 的检查脚本，只依赖 Python 标准库。

  python sg.py init <run_dir>                     生成 gate.json、manifest.json、REPORT.md 模板
  python sg.py freeze <run_dir>                   检查并冻结判据
  python sg.py freeze <run_dir> --amend "原因"    经用户同意后改判据，留下记录
  python sg.py scan <路径...>                     搜索已知的错误写法
  python sg.py readback <文件...> [--expect rules.json] [--list]
                                                  读已保存的模型（.mph）或导出的 JSON，按期望核对
  python sg.py preflight <run_dir> --script x     全部提交前检查，通过后写回执
  python sg.py submit <run_dir> --script x --sw comsol --name N [其他 simq 参数]
  python sg.py report <run_dir>                   检查报告并记录来源哈希
  python sg.py diagnose <job_id>                  按日志给失败任务分类
  python sg.py taint <目录> --reason "..."        标记有错的运行，并列出所有从它派生的运行

错误的分类和对应的检查见 references/failure-modes.md。
退出码：0 = 通过，1 = 检查不通过，2 = 用法或配置错误。
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile


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


HERE = pathlib.Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"
PROJECT_FILE = "simguard.project.json"
PLACEHOLDER = "TODO"

VALIDATION_KINDS = ("analytic_limit", "conservation", "independent_method", "literature_reproduction", "experiment")
GATE_OPS = ("<", "<=", ">", ">=", "==", "within")
REPORT_OVERALL = ("PASS", "PARTIAL", "FAIL", "BLOCKED")
REPORT_CHECK = ("PASS", "FAIL", "NOT_DONE", "NA")
REPORT_VERDICT = ("PASS", "FAIL", "NOT_DONE")
REQUIRED_CHECKS = ("resolution_convergence", "independent_validation", "readback")
NO_NA_CHECKS = ("independent_validation", "readback")   # 这两项不能写 NA
MODEL_SOLVERS = ("comsol", "lumerical", "fluent")       # 这些求解器有保存的模型，必须读回

# 只放跨项目都成立的规则；项目自己的规则写进 simguard.project.json。
BUILTIN_PATTERNS = [
    {"id": "comsol-partial-tensor", "glob": ["*.java", "*.py", "*.m"], "regex": r"""\.setIndex\(\s*["']c["']\s*,""",
     "why": T("setIndex 只设置张量的一个分量，其余分量保留默认值 1；要用 set(\"c\", 完整张量)",
              "setIndex sets only one tensor component; the others keep the default 1. Use set(\"c\", full tensor)")},
]
# 对所有 .mph 读回源默认生效（A1 结构量部分赋值的一般形式）；项目配置 builtin_readback=false 可关闭
BUILTIN_EXPECT = [
    {"select": "*:*", "op": "no_partial_default", "only_tensors": True, "min_matches": 0, "source_kind": "mph",
     "why": T("张量对角线上一部分是默认值 1、另一部分不是：常见于只设了一个分量",
              "tensor diagonal is partly the default 1 and partly not: usually only one component was set")},
]
BUILTIN_MARKERS = ["DO_NOT_REUSE*"]
TEXT_EXT = {".java", ".py", ".m", ".ps1", ".bat", ".cmd", ".lsf", ".jou", ".scm", ".txt"}
CODE_EXT = TEXT_EXT - {".txt"}                           # run 目录自动扫描时不读 .txt（可能是大日志）
SKIP_DIRS = {"__pycache__", ".simguard", "work", "recovery", ".git"}
MAX_SCAN_BYTES = 5 * 1024 * 1024
JOB_ID = re.compile(r"^\d{4}-\d{6}-[A-Za-z0-9]{1,12}-[a-z0-9]{3}$")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class UsageError(Exception):
    pass


def now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:   # PowerShell 5.1 写的 UTF-8 带 BOM
            return json.load(f)
    except json.JSONDecodeError as e:
        raise UsageError(T(f"{path} 不是合法 JSON：{e}", f"{path} is not valid JSON: {e}"))
    except FileNotFoundError:
        raise UsageError(T(f"找不到 {path}", f"not found: {path}"))


def write_json(path, data):
    """先写临时文件再替换，中途被杀也不会留下半个文件。"""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:      # Windows 上别的进程正在读
            time.sleep(0.1)
    os.replace(tmp, path)


def has_placeholder(value):
    if isinstance(value, str):
        return PLACEHOLDER in value
    if isinstance(value, dict):
        return any(has_placeholder(v) for v in value.values())
    if isinstance(value, list):
        return any(has_placeholder(v) for v in value)
    return False


def is_within(path, parent):
    try:
        pathlib.Path(path).resolve().relative_to(pathlib.Path(parent).resolve())
        return True
    except ValueError:
        return False


def parse_job_id(stdout):
    """simq submit 的输出里取 job id；警告等其他文字不会被当成 id。"""
    ids = [line.strip() for line in stdout.splitlines() if JOB_ID.match(line.strip())]
    return ids[-1] if ids else None


# ---------------------------------------------------------------- 项目配置

def load_project(start):
    """从 start 向上找 simguard.project.json；路径都相对配置文件所在目录。"""
    p = pathlib.Path(start).resolve()
    for d in [p] + list(p.parents):
        f = d / PROJECT_FILE
        if f.is_file():
            cfg = read_json(f)
            cfg["_root"] = str(d)
            return cfg
    return {"_root": None}


def project_paths(project, key):
    root = project.get("_root")
    return [pathlib.Path(root, x).resolve() for x in project.get(key, [])] if root else []


def forbidden_patterns(project):
    return BUILTIN_PATTERNS + project.get("forbidden_patterns", [])


def reuse_markers(project):
    return BUILTIN_MARKERS + project.get("do_not_reuse_markers", [])


# ---------------------------------------------------------------- 判据

def gate_errors(gate):
    errs = []
    if has_placeholder(gate):
        errs.append(T(f"gate.json 还有 {PLACEHOLDER} 没填", f"gate.json still has {PLACEHOLDER} to fill in"))
    if not gate.get("question"):
        errs.append(T("缺少 question", "missing question"))
    crit = gate.get("criteria") or []
    if not crit:
        errs.append(T("至少要有一条 criteria", "need at least one criteria entry"))
    ids = [str(c.get("id")) for c in crit]
    if len(set(ids)) != len(ids):
        errs.append(T("criteria 的 id 不能重复", "criteria ids must be unique"))
    for c in crit:
        for k in ("id", "metric", "op", "threshold"):
            if k not in c:
                errs.append(T(f"criteria 缺少 {k}：{c}", f"criteria missing {k}: {c}"))
        if c.get("op") not in GATE_OPS:
            errs.append(T(f"op 只能是 {GATE_OPS}：{c.get('id')}", f"op must be one of {GATE_OPS}: {c.get('id')}"))
    v = gate.get("validation") or {}
    if v.get("kind") not in VALIDATION_KINDS:
        errs.append(T(f"validation.kind 只能是 {VALIDATION_KINDS}；和旧结果一致不算独立校验",
                       f"validation.kind must be one of {VALIDATION_KINDS}; agreeing with old results is not independent validation"))
    if not v.get("description"):
        errs.append(T("validation.description 要写清校验方法", "validation.description must state the validation method"))
    if v.get("shares_code_with_model") is not False:
        errs.append(T("validation.shares_code_with_model 必须是 false：校验不能和被检模型共用同一段可能出错的设置",
                       "validation.shares_code_with_model must be false: the check must not share possibly wrong setup with the model"))
    return errs


def gate_lock_errors(run):
    run = pathlib.Path(run)
    gate_f, lock_f = run / "gate.json", run / "gate.lock.json"
    if not gate_f.is_file():
        return [T("缺少 gate.json", "missing gate.json")], None
    if not lock_f.is_file():
        return [T("判据没有冻结：先跑 sg.py freeze", "criteria not frozen: run sg.py freeze first")], None
    sha = sha256_file(gate_f)
    lock = read_json(lock_f)
    if lock.get("sha256") != sha:
        return [T("gate.json 在冻结后改过；改判据要经用户同意，再用 freeze --amend 记录",
                  "gate.json changed after freezing; changing criteria needs user approval, then record it with freeze --amend")], sha
    return [], sha


def submitted_gate_errors(run, current_sha):
    """提交时记下的判据哈希要和现在一致，或者有 amend 记录；否则说明冻结被删掉重来过。"""
    run = pathlib.Path(run)
    mf = run / "manifest.json"
    if not mf.is_file():
        return []
    manifest = read_json(mf)
    lock_f = run / "gate.lock.json"
    history = {h.get("sha256") for h in (read_json(lock_f).get("history", []) if lock_f.is_file() else [])}
    errs = []
    for job, sha in (manifest.get("job_gates") or {}).items():
        if sha and sha != current_sha and sha not in history:
            errs.append(T(f"任务 {job} 提交时的判据（{sha[:12]}）既不是现在的，也不在冻结历史里：冻结记录可能被删掉重建过",
                           f"job {job} was submitted with criteria ({sha[:12]}) that are neither current nor in the freeze history: "
                           f"the freeze record may have been deleted and recreated"))
    return errs


def cmd_freeze(a):
    run = pathlib.Path(a.run_dir)
    gate_f, lock_f = run / "gate.json", run / "gate.lock.json"
    if not gate_f.is_file():
        raise UsageError(T(f"找不到 {gate_f}", f"not found: {gate_f}"))
    errs = gate_errors(read_json(gate_f))
    if errs:
        return fail_list(T("判据不合格", "criteria invalid"), errs)
    sha = sha256_file(gate_f)
    history = []
    if lock_f.is_file():
        lock = read_json(lock_f)
        if lock.get("sha256") == sha:
            print(T(f"判据已冻结，未变化 sha256={sha[:12]}", f"criteria already frozen, unchanged sha256={sha[:12]}"))
            return 0
        if not a.amend:
            return fail_list(T("判据已冻结", "criteria already frozen"),
                             [T("内容已变化；只有用户同意后才能用 --amend \"原因\" 更新",
                                "content changed; update only with user approval via --amend \"reason\"")])
        history = lock.get("history", []) + [{"sha256": lock["sha256"], "frozen_at": lock.get("frozen_at"),
                                              "replaced_at": now_iso(), "reason": a.amend}]
    elif (run / "manifest.json").is_file() and (read_json(run / "manifest.json").get("jobs") or []):
        return fail_list(T("不能重新冻结", "cannot refreeze"),
                         [T("已经提交过任务，但冻结记录不见了；不要删掉 gate.lock.json 重来，告诉用户",
                            "jobs were already submitted but the freeze record is gone; do not delete gate.lock.json and redo, tell the user")])
    write_json(lock_f, {"sha256": sha, "frozen_at": now_iso(), "history": history})
    print(T(f"已冻结判据 sha256={sha[:12]}", f"criteria frozen sha256={sha[:12]}"))
    return 0


# ---------------------------------------------------------------- 错误写法扫描

def iter_files(paths, exts=TEXT_EXT):
    for p in paths:
        p = pathlib.Path(p)
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and f.suffix.lower() in exts and not SKIP_DIRS.intersection(f.relative_to(p).parts[:-1]):
                    yield f
        elif p.is_file():
            yield p


def run_code_files(run):
    """run 目录里所有代码文件：模板复制来的驱动不必逐个写进 manifest 也会被检查。"""
    return sorted(f for f in iter_files([run], CODE_EXT) if f.stat().st_size <= MAX_SCAN_BYTES)


def rule_applies(rule, name):
    globs = rule.get("glob", "*")
    return any(fnmatch.fnmatch(name, g) for g in ([globs] if isinstance(globs, str) else globs))


def scan_paths(paths, project):
    """返回 [(文件, 行号, 规则 id, 原因)]。整份文件匹配，跨行的写法也能抓到；
    匹配起始行里写 simguard: allow <id> 可以豁免一条。"""
    hits = []
    for f in iter_files(paths):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        for rule in forbidden_patterns(project):
            if not rule_applies(rule, f.name):
                continue
            for m in re.finditer(rule["regex"], text):
                no = text.count("\n", 0, m.start()) + 1
                line = lines[no - 1] if no <= len(lines) else ""
                if f"simguard: allow {rule['id']}" not in line:
                    hits.append((str(f), no, rule["id"], rule.get("why", "")))
    return hits


def cmd_scan(a):
    hits = scan_paths(a.paths, load_project(a.paths[0]))
    for f, no, rid, why in hits:
        print(f"{f}:{no} [{rid}] {why}")
    print(T(f"共 {len(hits)} 处", f"{len(hits)} hit(s)"))
    return 1 if hits else 0


# ---------------------------------------------------------------- 读回：把保存的文件摊平成 {键: 值}，再按声明的期望核对
#
# 读回源：.mph（直接读 zip 里的 dmodel.xml，不需要 COMSOL，也不占 license）或求解器导出的 JSON。
# .mph 的键：param/<名>、physics/<物理场>/<特征>/<参数>、material/<材料>/<属性组>/<参数>。
# JSON 的键：用点连接的路径，例如 source.wavelength_m。数字列表保留为一个值（向量或张量）。

QUOTED = re.compile(r"'((?:[^'\\]|\\.)*)'")


def parse_mph_value(raw):
    """COMSOL 的存值格式：N|M,'a','b',... 或 N|1,'a'|1,'b'|...。一个值返回字符串，多个返回列表。"""
    if not re.match(r"^\d+\|", raw or ""):
        return raw
    vals = [v.replace("\\'", "'").replace("\\,", ",") for v in QUOTED.findall(raw)]
    return vals[0] if len(vals) == 1 else vals


def flatten_mph(path):
    """返回 (键值, 元数据)。元数据记录每个物理场的类型，例如 physics/pde → CoefficientFormPDE。"""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("dmodel.xml"))
    flat, meta = {}, {}
    for e in root.iter("expressions"):
        if e.get("name"):
            flat.setdefault(f"param/{e.get('name')}", e.get("expr"))
    for phys in root.iter("Physics"):
        ptag = phys.get("tag")
        meta[f"physics/{ptag}"] = phys.get("op")
        for feat in phys.iter("PhysicsFeature"):
            for par in feat.findall("param"):
                flat[f"physics/{ptag}/{feat.get('tag')}/{par.get('param')}"] = parse_mph_value(par.get("value", ""))
    for mat in root.iter("Material"):
        for group in mat.iter("MaterialModel"):
            for par in group.findall("param"):
                flat[f"material/{mat.get('tag')}/{group.get('tag')}/{par.get('param')}"] = \
                    parse_mph_value(par.get("value", ""))
    return flat, meta


def flatten_json(data, prefix=""):
    flat = {}
    if isinstance(data, dict):
        for k, v in data.items():
            flat.update(flatten_json(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(data, list) and data and all(isinstance(x, (dict, list)) for x in data):
        for i, v in enumerate(data):
            flat.update(flatten_json(v, f"{prefix}.{i}" if prefix else str(i)))
    else:
        flat[prefix] = data
    return flat


def load_source(path):
    """返回 (类型, 键值, 元数据)。"""
    path = pathlib.Path(path)
    if path.suffix.lower() == ".mph":
        flat, meta = flatten_mph(path)
        return "mph", flat, meta
    if path.suffix.lower() == ".json":
        return "json", flatten_json(read_json(path)), {}
    raise UsageError(T(f"不支持的读回文件：{path.name}（支持 .mph 和求解器导出的 .json）",
                        f"unsupported readback file: {path.name} (supported: .mph and solver-exported .json)"))


UNIT = re.compile(r"\[[^\]]*\]")


def num(v):
    """数字或带单位的数字字符串（如 '2[W/m/K]'）转成 float；其他返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(UNIT.sub("", v).strip())
        except ValueError:
            return None
    return None


def norm(v):
    return re.sub(r"\s+", "", str(v))


def same(a, b):
    na, nb = num(a), num(b)
    if na is not None and nb is not None:
        return math.isclose(na, nb, rel_tol=1e-12, abs_tol=0.0)
    return norm(a) == norm(b)


def is_zero(v):
    n = num(v)
    return n == 0 if n is not None else norm(v) in ("0", "-0", "+0")


def as_matrix(v):
    """1、4、9 个分量的列表当作方阵；单个值当作 1×1。其他返回 None。"""
    vals = v if isinstance(v, list) else [v]
    n = {1: 1, 4: 2, 9: 3}.get(len(vals))
    return (vals, n) if n else (None, 0)


def diag_off(vals, n):
    return [vals[i * (n + 1)] for i in range(n)], [vals[i] for i in range(n * n) if i // n != i % n]


def check_value(rule, got):
    """单个值的比较。返回 None 表示通过，否则返回原因。"""
    op = rule["op"]
    want = rule.get("value")
    if op == "present":
        return None
    if op == "equals":
        return None if same(got, want) else T(f"期望等于 {want!r}", f"expected equal to {want!r}")
    if op == "differs":
        return None if not same(got, want) else T(f"期望不等于 {want!r}", f"expected not equal to {want!r}")
    if op == "one_of":
        return None if any(same(got, w) for w in want or []) else T(f"期望是 {want} 之一", f"expected one of {want}")
    if op in ("approx", "range", "nonzero", "finite"):
        vals = got if isinstance(got, list) else [got]
        nums = [num(x) for x in vals]
        if any(x is None for x in nums):
            return T("不是数字", "not a number")
        if op == "finite":
            return None if all(math.isfinite(x) for x in nums) else T("有 NaN 或无穷大", "has NaN or infinity")
        if op == "nonzero":
            return None if any(x != 0 for x in nums) else T("全为 0", "all zero")
        if op == "approx":
            w = num(want)
            if w is None:
                return T(f"期望值 {want!r} 不是数字", f"expected value {want!r} is not a number")
            tol = float(rule.get("rel", 1e-6)) * abs(w) + float(rule.get("abs", 0))
            return None if all(abs(x - w) <= tol for x in nums) else T(f"期望约等于 {want}（rel={rule.get('rel', 1e-6)}）",
                                                                      f"expected approx {want} (rel={rule.get('rel', 1e-6)})")
        lo, hi = rule.get("min"), rule.get("max")
        ok = all((lo is None or x >= lo) and (hi is None or x <= hi) for x in nums)
        return None if ok else T(f"期望在 [{lo}, {hi}] 内", f"expected within [{lo}, {hi}]")
    if op in ("isotropic", "diagonal", "symmetric", "no_partial_default"):
        vals, n = as_matrix(got)
        if not n:
            return T("不是 1、4 或 9 个分量的张量", "not a tensor with 1, 4 or 9 components")
        diag, off = diag_off(vals, n)
        if op == "isotropic":
            if not all(same(d, diag[0]) for d in diag) or is_zero(diag[0]) or not all(is_zero(x) for x in off):
                return T("期望各向同性：对角线相等且非零，其余为 0", "expected isotropic: equal nonzero diagonal, zeros elsewhere")
        elif op == "diagonal":
            if not all(is_zero(x) for x in off):
                return T("期望对角张量：非对角分量为 0", "expected diagonal tensor: off-diagonal components zero")
        elif op == "symmetric":
            if not all(same(vals[i * n + j], vals[j * n + i]) for i in range(n) for j in range(n)):
                return T("期望对称张量", "expected symmetric tensor")
        else:
            default = rule.get("default", "1")
            flags = [same(d, default) for d in diag]
            if n > 1 and any(flags) and not all(flags):
                return T(f"对角线上一部分是默认值 {default}、另一部分不是：可能只设了一个分量",
                         f"diagonal is partly the default {default} and partly not: maybe only one component was set")
        return None
    raise UsageError(T(f"未知的比较方式：{op}", f"unknown comparison op: {op}"))


AGG_OPS = ("sum_approx", "converged")


def check_aggregate(rule, sources):
    """跨多个键的比较：守恒闭合（sum_approx）和分辨率收敛（converged）。"""
    keys = rule.get("keys") or []
    if len(keys) < 2:
        return [T(f"{rule['op']} 至少要列两个 keys", f"{rule['op']} needs at least two keys")]
    vals = []
    for key in keys:
        src, _, k = key.partition(":")
        flat = sources.get(src, (None, {}, {}))[1]
        if k not in flat or num(flat[k]) is None:
            return [T(f"{key}：读回里没有这个数字", f"{key}: no such number in the readback")]
        vals.append(num(flat[k]))
    if not all(math.isfinite(v) for v in vals):
        return [T(f"{keys}：有 NaN 或无穷大", f"{keys}: has NaN or infinity")]
    if rule["op"] == "sum_approx":
        want = float(rule["value"])
        tol = float(rule.get("rel", 1e-3)) * abs(want) + float(rule.get("abs", 0))
        total = sum(vals)
        return [] if abs(total - want) <= tol else [T(f"{keys} 之和 {total:g}，期望 {want:g}±{tol:g}（守恒没闭合）",
                                                        f"{keys} sum to {total:g}, expected {want:g}±{tol:g} (conservation not closed)")]
    last, prev = vals[-1], vals[-2]
    change = abs(last - prev) / max(abs(last), 1e-300)
    rel = float(rule.get("rel", 0.01))
    errs = [] if change <= rel else [T(f"{keys} 最后两级相对变化 {change:.3g}，超过 {rel}（没收敛）",
                                          f"{keys} last two levels differ by {change:.3g}, more than {rel} (not converged)")]
    if all(v == vals[0] for v in vals):
        errs.append(T(f"{keys} 各级结果完全相同：收敛检查可能不可能失败（离散格式对测试解精确，或分辨率没真的改变）",
                       f"{keys} are identical at all levels: the convergence check may be unable to fail "
                       f"(scheme exact for the test solution, or resolution did not really change)"))
    return errs


def evaluate_expect(rules, sources, allow=()):
    """sources = {名字: (类型, 键值, 元数据)}。返回 (错误, 检查过的条数)。"""
    allowed = [a.get("key", "") for a in allow]
    errs, checked = [], 0
    for rule in rules:
        if rule.get("op") in AGG_OPS:
            errs += check_aggregate(rule, sources)
            checked += 1
            continue
        src_pat, _, key_pat = rule["select"].partition(":")
        if not key_pat:
            raise UsageError(T(f"select 要写成 源名:键，例如 model:param/k；收到 {rule['select']}",
                                f"select must be source:key, e.g. model:param/k; got {rule['select']}"))
        matched = 0
        for sname, (kind, flat, meta) in sources.items():
            if not fnmatch.fnmatchcase(sname, src_pat) or rule.get("source_kind", kind) != kind:
                continue
            for key, got in flat.items():
                if not fnmatch.fnmatchcase(key, key_pat):
                    continue
                ops = rule.get("physics_op")
                if ops and meta.get("/".join(key.split("/")[:2])) not in ops:
                    continue
                if rule.get("only_tensors") and not (isinstance(got, list) and as_matrix(got)[1] > 1):
                    continue
                matched += 1
                full = f"{sname}:{key}"
                why = check_value(rule, got)
                if why and not any(fnmatch.fnmatchcase(full, a) for a in allowed):
                    errs.append(T(f"{full}：读回 {got!r}，{rule.get('why') or why}", f"{full}: read back {got!r}, {rule.get('why') or why}"))
        checked += matched
        if matched < rule.get("min_matches", 1):
            errs.append(T(f"{rule['select']} 没有匹配到任何值：可能是键写错了，不能当作没有问题",
                           f"{rule['select']} matched no values: the key may be wrong; this is not a pass"))
    return errs, checked


def legacy_readback(rb, project):
    """把旧写法（comsol_tensor_models、comsol_params、dump + checks）转成读回源和期望。"""
    sources, rules = {}, []
    conf = project.get("comsol_tensor", {})
    tags = conf.get("physics_tags")
    for i, m in enumerate(rb.get("comsol_tensor_models") or []):
        name = f"m{i}"
        sources[name] = m
        selects = [f"{name}:physics/{t}/*/c" for t in tags] if tags else [f"{name}:physics/*/*/c"]
        for sel in selects:
            rule = {"select": sel, "op": "isotropic", "min_matches": 0 if tags else 1}
            if not tags:
                rule["physics_op"] = ["CoefficientFormPDE"]
            rules.append(rule)
        if tags:
            rules.append({"op": "present", "select": f"{name}:physics/*/*/c", "min_matches": 1})
        for pname, want in (rb.get("comsol_params") or {}).items():   # 每个模型都要核对
            rules.append({"select": f"{name}:param/{pname}", "op": "equals", "value": want})
    if rb.get("dump"):
        sources["dump"] = rb["dump"]
        for c in rb.get("checks") or []:
            rule = {k: v for k, v in c.items() if k != "path"}
            rule["select"] = f"dump:{c.get('path')}"
            rules.append(rule)
    allow = [{"key": f"*:physics/{x.get('physics')}/{x.get('feature')}/c", "reason": x.get("reason", "")}
             for x in rb.get("anisotropic_ok") or []]
    return sources, rules, allow


def readback_plan(rb, project):
    """合并新旧写法，返回 (源路径, 期望, 豁免, 错误)。"""
    errs = []
    sources, rules, allow = legacy_readback(rb, project)
    if rb.get("comsol_params") and not rb.get("comsol_tensor_models"):
        errs.append(T("comsol_params 要配合 comsol_tensor_models 使用，或改用 sources + expect",
                       "comsol_params needs comsol_tensor_models, or use sources + expect instead"))
    if rb.get("dump") and not rb.get("checks"):
        errs.append(T("readback.dump 没有配 checks，读回等于没查", "readback.dump has no checks, so nothing is checked"))
    sources.update(rb.get("sources") or {})
    rules += rb.get("expect") or []
    allow += rb.get("allow") or []
    for a in allow:
        if not a.get("reason"):
            errs.append(T(f"readback.allow 的每一项都要写 reason：{a}", f"every readback.allow entry needs a reason: {a}"))
    if project.get("builtin_readback", True):
        rules += BUILTIN_EXPECT
    rules += [dict(r, min_matches=r.get("min_matches", 0)) for r in project.get("readback_expect", [])]
    return sources, rules, allow, errs


def load_sources(base, paths):
    """返回 ({名字: (类型, 键值, 元数据)}, 错误)。"""
    loaded, errs = {}, []
    for name, rel in paths.items():
        p = pathlib.Path(base) / rel
        if not p.is_file():
            errs.append(T(f"读回文件不存在：{rel}", f"readback file not found: {rel}"))
            continue
        try:
            loaded[name] = load_source(p)
        except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as e:
            errs.append(T(f"{rel} 读不出模型设置：{e}", f"cannot read model settings from {rel}: {e}"))
        except UsageError as e:
            errs.append(str(e))
    return loaded, errs


def readback_errors(run, manifest, project):
    rb = manifest.get("readback")
    if not isinstance(rb, dict):
        return [T("manifest.readback 要写明读回方式；确实不需要时写 {\"not_applicable\": \"原因\"}",
                  "manifest.readback must state the readback method; if truly not needed write {\"not_applicable\": \"reason\"}")]
    if rb.get("not_applicable"):
        if manifest.get("solver") in MODEL_SOLVERS:
            return [T(f"{manifest['solver']} 模型不能跳过读回：用 sources + expect 读回已保存的模型或导出的 JSON",
                      f"{manifest['solver']} models cannot skip readback: use sources + expect on the saved model or exported JSON")]
        return []
    paths, rules, allow, errs = readback_plan(rb, project)
    if not paths:
        return errs + [T("manifest.readback 既没有模型也没有读回文件", "manifest.readback has neither models nor readback files")]
    if not [r for r in rules if r not in BUILTIN_EXPECT]:
        errs.append(T("readback 没有写任何期望（expect），读回等于没查", "readback has no expect rules, so nothing is checked"))
    sources, load_errs = load_sources(run, paths)
    errs += load_errs
    if sources:
        rule_errs, checked = evaluate_expect(rules, sources, allow)
        errs += rule_errs
        if not checked:
            errs.append(T("读回没有检查到任何值", "readback checked no values"))
    return errs


def cmd_readback(a):
    rules = read_json(a.expect) if a.expect else []
    project = load_project(a.files[0])
    if not a.list and not rules and not project.get("builtin_readback", True):
        raise UsageError(T("没有可用的检查：给 --expect，或开启内置规则", "no checks available: pass --expect or enable the built-in rules"))
    total_errs, total_checked = [], 0
    for i, f in enumerate(a.files):
        try:
            kind, flat, meta = load_source(f)
        except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as e:
            raise UsageError(T(f"{f} 读不出模型设置（文件被占用时等求解结束再读）：{e}",
                                f"cannot read model settings from {f} (if the file is in use, wait for the solve to finish): {e}"))
        name = pathlib.Path(f).stem
        if a.list:
            for k, v in flat.items():
                print(f"{name}:{k} = {v!r}")
            continue
        file_rules = [dict(r, select=r["select"].replace("*:", f"{name}:", 1)) if "select" in r else r for r in rules]
        if project.get("builtin_readback", True):
            file_rules += BUILTIN_EXPECT
        errs, checked = evaluate_expect(file_rules, {name: (kind, flat, meta)})
        tensors = sum(1 for v in flat.values() if isinstance(v, list) and as_matrix(v)[1] > 1)
        print(T(f"{f}：{len(flat)} 个值，其中张量 {tensors} 个；检查了 {checked} 个",
                f"{f}: {len(flat)} values, {tensors} tensors; {checked} checked"))
        for e in errs:
            print(f"  - {e}")
        total_errs += errs
        total_checked += checked
    if a.list:
        return 0
    if not total_checked:
        print(T("没有检查到任何值：用 --list 看有哪些键，再用 --expect 写期望。没检查不等于没问题。",
                "no values checked: use --list to see the keys, then write rules with --expect. Unchecked is not OK."))
        return 1
    return 1 if total_errs else 0


# ---------------------------------------------------------------- 响应测试：改一个输入，输出要按预期变化

RESPONSE_EXPECT = ("changes", "unchanged", "increases", "decreases", "ratio")


def response_value(v, run):
    """数字，或 {"file": "x.json", "key": "a.b"}：从这次运行的结果文件里读，而不是手抄。"""
    if isinstance(v, dict) and "file" in v:
        flat = flatten_json(read_json(pathlib.Path(run) / v["file"]))
        return num(flat.get(v.get("key", "")))
    return num(v)


def response_errors(run, items, label=None):
    label = label or T("响应测试", "response test")
    errs = []
    for t in items or []:
        tid = t.get("id") or t.get("knob") or t.get("switch") or "?"
        if "baseline" not in t and "perturbed" not in t:
            if t.get("status") != "PASS":            # 旧写法：只有状态
                errs.append(T(f"{label}未通过：{t}", f"{label} failed: {t}"))
            continue
        exp = t.get("expect")
        if exp not in RESPONSE_EXPECT:
            errs.append(T(f"{label} {tid}：expect 只能是 {RESPONSE_EXPECT}", f"{label} {tid}: expect must be one of {RESPONSE_EXPECT}"))
            continue
        try:
            b, p = response_value(t.get("baseline"), run), response_value(t.get("perturbed"), run)
        except UsageError as e:
            errs.append(T(f"{label} {tid}：{e}", f"{label} {tid}: {e}"))
            continue
        if b is None or p is None or not (math.isfinite(b) and math.isfinite(p)):
            errs.append(T(f"{label} {tid}：baseline 和 perturbed 都要是有限的数字（没做就是没做）",
                           f"{label} {tid}: baseline and perturbed must both be finite numbers (not done means not done)"))
            continue
        rel = float(t.get("rel", 0.01))
        delta, scale = p - b, max(abs(b), 1e-300)
        ok = {"changes": abs(delta) > rel * scale, "unchanged": abs(delta) <= rel * scale,
              "increases": delta > rel * scale, "decreases": -delta > rel * scale}.get(exp)
        if exp == "ratio":
            lo, hi = (t.get("ratio") or [None, None])[:2]
            r = p / b if b else math.inf
            ok = (lo is None or r >= lo) and (hi is None or r <= hi)
        if not ok:
            ratio = ' ' + str(t.get('ratio')) if exp == 'ratio' else ''
            knob = t.get('knob') or t.get('switch', '')
            errs.append(T(f"{label} {tid}（{knob}）：baseline={b:g}，perturbed={p:g}，期望 {exp}{ratio}",
                          f"{label} {tid} ({knob}): baseline={b:g}, perturbed={p:g}, expected {exp}{ratio}"))
    return errs


# ---------------------------------------------------------------- preflight

def marker_dirs(src, project):
    """来源所在目录以及它的各级上级目录（到项目根为止）。"""
    d = src if src.is_dir() else src.parent
    root = pathlib.Path(project["_root"]).resolve() if project.get("_root") else None
    dirs = [d]
    for parent in d.resolve().parents:
        if root and not is_within(parent, root):
            break
        dirs.append(parent)
    return dirs


def provenance_errors(run, manifest, project, script):
    errs = []
    approved = project_paths(project, "approved_templates")
    for src in [run / d for d in manifest.get("derived_from") or []]:
        if not src.exists():
            errs.append(T(f"derived_from 不存在：{src}", f"derived_from does not exist: {src}"))
            continue
        hit = None
        for d in marker_dirs(src, project):
            for marker in reuse_markers(project):
                found = list(d.glob(marker))
                if found:
                    hit = found[0]
                    break
            if hit:
                break
        if hit:
            errs.append(T(f"{src} 来自标记了不可复用的目录：{hit}", f"{src} comes from a directory marked do-not-reuse: {hit}"))
        if approved and not any(src.resolve() == t or is_within(src, t) for t in approved):
            errs.append(T(f"{src} 不在 approved_templates 里", f"{src} is not in approved_templates"))
    for d in [script] + [run / d for d in manifest.get("drivers") or []]:
        if not pathlib.Path(d).exists():
            errs.append(T(f"驱动脚本不存在：{d}", f"driver script does not exist: {d}"))
    for f, no, rid, why in scan_paths(scan_targets(run, manifest, script), project):
        errs.append(f"{f}:{no} [{rid}] {why}")
    return errs


def scan_targets(run, manifest, script):
    targets = {pathlib.Path(script).resolve()} if pathlib.Path(script).is_file() else set()
    targets |= {(run / d).resolve() for d in manifest.get("drivers") or [] if (run / d).is_file()}
    targets |= {f.resolve() for f in run_code_files(run)}
    return sorted(targets)


def scope_errors(run, project):
    errs = []
    for ro in project_paths(project, "readonly_paths"):
        if is_within(run, ro):
            errs.append(T(f"run 目录在只读范围内：{ro}", f"run directory is inside a read-only path: {ro}"))
    return errs


def status_list_errors(items, label):
    errs = []
    for item in items or []:
        if item.get("status") != "PASS":
            errs.append(T(f"{label}未通过：{item}", f"{label} failed: {item}"))
    return errs


def manifest_errors(manifest):
    errs = []
    if has_placeholder(manifest):
        errs.append(T(f"manifest.json 还有 {PLACEHOLDER} 没填", f"manifest.json still has {PLACEHOLDER} to fill in"))
    for k in ("run_id", "line", "solver"):
        if not manifest.get(k):
            errs.append(T(f"manifest 缺少 {k}", f"manifest missing {k}"))
    res = manifest.get("resources") or {}
    for k in ("cores", "mem_gb", "basis"):
        if not res.get(k):
            errs.append(T(f"manifest.resources 缺少 {k}；basis 写内存估算依据，例如小测试实测",
                           f"manifest.resources missing {k}; basis states how memory was estimated, e.g. measured in a small test"))
    resume = manifest.get("resume") or {}
    if "supported" not in resume or (resume.get("supported") and not resume.get("how")) or \
            (resume.get("supported") is False and not resume.get("why_not")):
        errs.append(T("manifest.resume 要写 supported，并给出 how 或 why_not", "manifest.resume needs supported, plus how or why_not"))
    return errs


def lumapi_errors(run, manifest, script):
    if manifest.get("solver") == "lumerical":
        return []
    errs = []
    for src in scan_targets(run, manifest, script):
        if src.suffix.lower() == ".py" and \
                re.search(r"^\s*(import|from)\s+lumapi\b", src.read_text(encoding="utf-8", errors="replace"), re.M):
            errs.append(T(f"{src.name} 调用 lumapi，manifest.solver 必须是 lumerical，否则绕过 license 排队",
                           f"{src.name} uses lumapi, so manifest.solver must be lumerical; otherwise it bypasses license queuing"))
    return errs


def preflight(run, script, sw=None):
    """返回 ([(检查项, [错误])], gate 哈希)。"""
    if not (run / "manifest.json").is_file():
        return [("manifest", [T("缺少 manifest.json：先跑 sg.py init", "missing manifest.json: run sg.py init first")])], None
    manifest = read_json(run / "manifest.json")
    project = load_project(run)
    lock_errs, gate_sha = gate_lock_errors(run)
    gate = run / "gate.json"
    gate_errs = gate_errors(read_json(gate)) if gate.is_file() else []
    solver_errs = lumapi_errors(run, manifest, script)
    if sw and manifest.get("solver") and sw != manifest["solver"]:
        solver_errs.append(T(f"--sw {sw} 和 manifest.solver {manifest['solver']} 不一致",
                              f"--sw {sw} does not match manifest.solver {manifest['solver']}"))
    return [
        (T("清单完整", "manifest complete"), manifest_errors(manifest)),
        (T("工作范围", "work scope"), scope_errors(run, project)),
        (T("判据冻结", "criteria frozen"), lock_errs + gate_errs),
        (T("求解器类型", "solver type"), solver_errs),
        (T("驱动来源和错误写法", "driver provenance and bad patterns"), provenance_errors(run, manifest, project, script)),
        (T("模型读回", "model readback"), readback_errors(run, manifest, project)),
        (T("开关和响应测试", "switch and response tests"), response_errors(run, manifest.get("switch_tests"), T("开关测试", "switch test")) +
         response_errors(run, manifest.get("response_tests"))),
        (T("文献公式复现", "literature formula reproduction"), status_list_errors(manifest.get("literature"), T("文献复现", "literature reproduction"))),
    ], gate_sha


def receipt_inputs(run, manifest, script):
    """回执覆盖的文件：脚本、run 目录里的代码、判据和读回文件。提交后改了任何一个，simq 都会拒绝。"""
    files = set(scan_targets(run, manifest, script))
    files.add((run / "gate.json").resolve())
    rb = manifest.get("readback") if isinstance(manifest.get("readback"), dict) else {}
    paths, _, _, _ = readback_plan(rb, load_project(run))
    files |= {(run / p).resolve() for p in paths.values() if (run / p).is_file()}
    out = {}
    for f in sorted(files):
        if f.is_file():
            key = f.relative_to(run.resolve()).as_posix() if is_within(f, run) else str(f)
            out[key] = sha256_file(f)
    return out


def run_preflight(run, script, sw=None):
    """跑检查、写回执。放在 run 目录的 .simguard/ 下，simq 会核对。"""
    results, gate_sha = preflight(run, script, sw)
    passed = all(not errs for _, errs in results)
    for name, errs in results:
        print(f"[{'PASS' if not errs else 'FAIL'}] {name}")
        for e in errs:
            print(f"       - {e}")
    receipt = run / ".simguard" / "receipt.json"
    if passed:
        manifest = read_json(run / "manifest.json")
        write_json(receipt, {"status": "PASS", "created": now_iso(), "script": str(script.resolve()),
                             "script_sha256": sha256_file(script), "gate_sha256": gate_sha,
                             "inputs": receipt_inputs(run, manifest, script)})
        print(T(f"通过，回执：{receipt}", f"passed, receipt: {receipt}"))
    else:
        if receipt.is_file():
            receipt.unlink()   # 旧回执不再有效
        print(T("不通过，不能提交。修复问题，不要绕过检查。", "failed, cannot submit. Fix the problems; do not bypass the checks."))
    return passed, gate_sha


def resolve_script(run, script):
    """相对路径按 run 目录解析，与 manifest 里的路径一致。"""
    p = pathlib.Path(script)
    return p if p.is_absolute() else pathlib.Path(run) / p


def cmd_preflight(a):
    run = pathlib.Path(a.run_dir).resolve()
    return 0 if run_preflight(run, resolve_script(run, a.script), a.sw)[0] else 1


def find_simq(a, project):
    candidates = [getattr(a, "simq", None), os.environ.get("SIMQ_PY")]
    if project.get("simq_py") and project.get("_root"):
        candidates.append(str(pathlib.Path(project["_root"], project["simq_py"])))
    for c in candidates:
        if c and pathlib.Path(c).is_file():
            return c
    raise UsageError(T("找不到 simq.py：用 --simq、环境变量 SIMQ_PY 或项目配置 simq_py 指定。不要改为直接启动求解器。",
                        "simq.py not found: set it with --simq, env SIMQ_PY or project config simq_py. Do not start the solver directly instead."))


def record_job(run, job_id, gate_sha=None, key="jobs"):
    manifest = read_json(run / "manifest.json")
    manifest[key] = (manifest.get(key) or []) + [job_id]
    if gate_sha:
        manifest.setdefault("job_gates", {})[job_id] = gate_sha
    write_json(run / "manifest.json", manifest)


def cmd_submit(a, extra):
    run = pathlib.Path(a.run_dir).resolve()
    script = resolve_script(run, a.script).resolve()
    simq = find_simq(a, load_project(run))
    passed, gate_sha = run_preflight(run, script, a.sw)
    if not passed:
        return 1
    manifest = read_json(run / "manifest.json")
    res = manifest["resources"]
    argv = [sys.executable, simq, "submit", "--line", manifest["line"], "--name", a.name or manifest["run_id"],
            "--sw", a.sw, "--cores", str(res["cores"]), "--mem", str(res["mem_gb"]),
            "--cwd", str(run), "--script", str(script)] + extra
    done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    if done.returncode:
        return done.returncode
    job_id = parse_job_id(done.stdout)
    if not job_id:
        raise UsageError(T(f"simq 没有返回 job id，请用 simq list 确认是否已提交，不要重复提交：{done.stdout.strip()}",
                           f"simq returned no job id; check with simq list whether it was submitted, do not submit again: "
                           f"{done.stdout.strip()}"))
    record_job(run, job_id, gate_sha)
    return 0


# ---------------------------------------------------------------- 失败分类

# 按顺序匹配。只有 license 这类暂时性问题适合自动重试；其他原因重试只会再失败一次。
FAILURE_CLASSES = [
    ("memory", r"out of memory|outofmemory|memory allocation|insufficient memory|内存不足|超过声明内存|exceeded declared memory"),
    ("license", r"licen[sc]e.*(fail|error|unavailable|denied|could not|not available|checkout)"
                r"|(fail|could not|unable|cannot).*licen[sc]e|license 占满|license pool full"),
    ("convergence", r"not converge|failed to find a solution|diverge|发散|nan detected"),
    ("config", r"error running java|traceback|syntaxerror|no such file|cannot find|not found|编译失败|不存在|compile failed|does not exist"),
]
FAILURE_ADVICE = {
    "memory": T("减少线程或模型规模，并更新内存估计；不要原样重试",
                "reduce threads or model size and update the memory estimate; do not retry as is"),
    "license": T("license 暂时不可用，可以重试", "license temporarily unavailable; retry is OK"),
    "convergence": T("先诊断物理和数值设置；同一问题最多重试 2 次",
                     "diagnose the physics and numerical settings first; at most 2 retries for the same problem"),
    "config": T("修复脚本或配置；不要原样重试", "fix the script or config; do not retry as is"),
    "unknown": T("读日志诊断", "read the log to diagnose"),
}
RETRYABLE = ("license",)


def classify_failure(text):
    for name, pattern in FAILURE_CLASSES:
        match = re.search(pattern, text, re.I)
        if match:
            return name, match[0]
    return "unknown", ""


def simq_call(simq, *args):
    return subprocess.run([sys.executable, simq, *map(str, args)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def cmd_diagnose(a):
    simq = find_simq(a, load_project(os.getcwd()))
    done = simq_call(simq, "tail", a.job_id, "-n", 200)
    if done.returncode:
        raise UsageError(done.stderr.strip() or T("读不到日志", "cannot read the log"))
    name, evidence = classify_failure(done.stdout)
    print(T(f"类别：{name}\n证据：{evidence or '无'}\n建议：{FAILURE_ADVICE[name]}",
            f"class: {name}\nevidence: {evidence or 'none'}\nadvice: {FAILURE_ADVICE[name]}"))
    return 0


# ---------------------------------------------------------------- 结果文件

def result_sources(run, manifest):
    """结果期望读哪些文件：manifest.result_sources，默认 outputs 里的 JSON。"""
    if manifest.get("result_sources"):
        return dict(manifest["result_sources"])
    return {pathlib.Path(o).stem: o for o in manifest.get("outputs") or [] if str(o).lower().endswith(".json")}


def nonfinite_errors(sources):
    """结果里的 NaN 或无穷大：求解失败时常见，退出码却可能是 0。"""
    errs = []
    for name, (kind, flat, _) in sources.items():
        if kind != "json":
            continue
        for key, v in flat.items():
            vals = v if isinstance(v, list) else [v]
            if any(isinstance(x, float) and not math.isfinite(x) for x in vals):
                errs.append(T(f"{name}:{key} 有 NaN 或无穷大", f"{name}:{key} has NaN or infinity"))
    return errs


def result_errors(run, manifest):
    """运行后的检查：结果文件能读、没有 NaN，并满足 result_expect。"""
    paths = result_sources(run, manifest)
    sources, errs = load_sources(run, paths)
    errs += nonfinite_errors(sources)
    rules = manifest.get("result_expect") or []
    if rules and sources:
        rule_errs, _ = evaluate_expect(rules, sources)
        errs += rule_errs
    return errs


# ---------------------------------------------------------------- 报告

def parse_report(text):
    """取 Overall 状态、检查表和判据表。

    检查表行：| check | 状态 | 证据 |。判据表行：| id | 实测值 | 阈值 | 结论 |，id 要和 gate.json 完全一致。"""
    m = re.search(r"^Overall:\s*(\S+)", text, re.M)
    overall = m[1] if m else None
    checks, rows = {}, []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[1] in REPORT_CHECK:
            checks[cells[0]] = (cells[1], cells[2])
        rows.append(cells)
    return overall, checks, rows


def criteria_errors(rows, criteria, overall):
    errs = []
    verdicts = {}
    for c in criteria:
        cid = str(c.get("id"))
        mine = [r for r in rows if r and r[0] == cid]
        if len(mine) != 1:
            errs.append(T(f"“判据结果”表里判据 {cid} 要正好一行（现在 {len(mine)} 行）：写 id、实测值、阈值和结论",
                           f"criterion {cid} needs exactly one row in the criteria results table (now {len(mine)}): "
                           f"id, measured value, threshold, verdict"))
            continue
        verdict = mine[0][-1].upper()
        if verdict not in REPORT_VERDICT:
            errs.append(T(f"判据 {cid} 的结论只能是 {REPORT_VERDICT}，现在是“{mine[0][-1]}”",
                           f"verdict of criterion {cid} must be one of {REPORT_VERDICT}, got \"{mine[0][-1]}\""))
            continue
        verdicts[cid] = verdict
    bad = [k for k, v in verdicts.items() if v != "PASS"]
    if overall == "PASS" and bad:
        errs.append(T(f"Overall 不能写 PASS：判据 {bad} 没通过或没做", f"Overall cannot be PASS: criteria {bad} failed or not done"))
    return errs


def report_errors(run, project):
    f = run / "REPORT.md"
    if not f.is_file():
        return [T("缺少 REPORT.md", "missing REPORT.md")]
    text = f.read_text(encoding="utf-8-sig")
    errs = []
    if PLACEHOLDER in text:
        errs.append(T(f"REPORT.md 还有 {PLACEHOLDER} 没填；按模板填写所有段落", f"REPORT.md still has {PLACEHOLDER}; fill in every section of the template"))
    overall, checks, rows = parse_report(text)
    if overall not in REPORT_OVERALL:
        errs.append(T(f"Overall 只能是 {REPORT_OVERALL}", f"Overall must be one of {REPORT_OVERALL}"))
    required = list(REQUIRED_CHECKS) + list(project.get("required_report_checks", []))
    for name in required:
        if name not in checks:
            errs.append(T(f"检查表缺少 {name}", f"checks table missing {name}"))
    for name, (status, evidence) in checks.items():
        if status in ("PASS", "NA") and not evidence:
            errs.append(T(f"{name} 标了 {status}，但没写证据或原因", f"{name} is {status} but has no evidence or reason"))
        if status == "NA" and name in NO_NA_CHECKS:
            errs.append(T(f"{name} 不能写 NA：没做就写 NOT_DONE", f"{name} cannot be NA: write NOT_DONE if not done"))
    bad = [n for n in required if checks.get(n, ("",))[0] in ("NOT_DONE", "FAIL")]
    if overall == "PASS" and bad:
        errs.append(T(f"Overall 不能写 PASS：{bad} 没做或没通过；应写 PARTIAL 或 FAIL", f"Overall cannot be PASS: {bad} not done or failed; use PARTIAL or FAIL"))
    gate_f = run / "gate.json"
    if gate_f.is_file():
        errs += criteria_errors(rows, read_json(gate_f).get("criteria", []), overall)
    mf = run / "manifest.json"
    if mf.is_file():
        res_errs = result_errors(run, read_json(mf))
        if res_errs and overall == "PASS":
            errs += [T(f"结果检查不通过，Overall 不能写 PASS：{e}", f"result check failed, Overall cannot be PASS: {e}") for e in res_errs]
        elif res_errs:
            print(T("结果检查发现问题（已在报告中说明的话可以忽略）：", "result check found problems (ignore if already explained in the report):"))
            for e in res_errs:
                print(f"  - {e}")
    return errs


def cmd_report(a):
    run = pathlib.Path(a.run_dir)
    if not (run / "manifest.json").is_file():
        return fail_list(T("报告不合格", "report invalid"), [T("缺少 manifest.json", "missing manifest.json")])
    errs = report_errors(run, load_project(run))
    lock_errs, gate_sha = gate_lock_errors(run)
    errs += lock_errs
    if gate_sha:
        errs += submitted_gate_errors(run, gate_sha)
    if errs:
        return fail_list(T("报告不合格", "report invalid"), errs)
    manifest = read_json(run / "manifest.json")
    if not manifest.get("jobs"):
        print(T("提醒：manifest.jobs 里没有 job id", "note: manifest.jobs has no job id"))
    rb = manifest.get("readback") if isinstance(manifest.get("readback"), dict) else {}
    rb_paths = list(readback_plan(rb, load_project(run))[0].values())
    files = {}
    for rel in (manifest.get("drivers") or []) + (manifest.get("outputs") or []) + rb_paths:
        p = run / rel
        files[rel] = sha256_file(p) if p.is_file() else None
    missing = [k for k, v in files.items() if v is None]
    write_json(run / "provenance.json", {"created": now_iso(), "gate_sha256": gate_sha,
                                         "jobs": manifest.get("jobs") or [], "derived_from": manifest.get("derived_from") or [],
                                         "files": files})
    if missing:
        return fail_list(T("来源文件缺失", "source files missing"), missing)
    print(T("报告合格，已写 provenance.json", "report OK, wrote provenance.json"))
    return 0


# ---------------------------------------------------------------- 污染追踪：一个运行有错，找出所有从它派生的运行

def find_runs(root):
    for mf in pathlib.Path(root).rglob("manifest.json"):
        if not SKIP_DIRS.intersection(mf.relative_to(root).parts):
            yield mf.parent


def run_inputs(run):
    try:
        manifest = read_json(run / "manifest.json")
    except UsageError:
        return []
    return [(run / p).resolve() for p in (manifest.get("derived_from") or []) + (manifest.get("drivers") or [])]


def cmd_taint(a):
    bad = pathlib.Path(a.path).resolve()
    if bad.is_file():
        bad = bad.parent
        print(T(f"标记以目录为单位：{bad}", f"marking works per directory: {bad}"))
    if not bad.is_dir():
        raise UsageError(T(f"找不到目录：{a.path}", f"directory not found: {a.path}"))
    project = load_project(bad)
    root = pathlib.Path(a.root or project.get("_root") or bad.parent).resolve()
    marker = bad / "DO_NOT_REUSE.md"
    if not marker.exists():
        marker.write_text(T(f"# 不可复用\n\n{now_iso()}\n\n{a.reason}\n", f"# Do not reuse\n\n{now_iso()}\n\n{a.reason}\n"),
                          encoding="utf-8")
        print(T(f"已写 {marker}", f"wrote {marker}"))
    tainted, affected = [bad], []
    runs = [r for r in find_runs(root) if r.resolve() != bad]
    changed = True
    while changed:                     # 传递闭包：派生运行的派生运行也受影响
        changed = False
        for r in runs:
            if r in affected:
                continue
            if any(is_within(i, t) for i in run_inputs(r) for t in tainted):
                affected.append(r)
                tainted.append(r.resolve())
                changed = True
    print(T(f"在 {root} 下找到 {len(affected)} 个受影响的运行：", f"found {len(affected)} affected run(s) under {root}:"))
    for r in affected:
        prov = r / "provenance.json"
        outs = list(read_json(prov).get("files", {})) if prov.is_file() else []
        print(f"  {r}" + (T(f"  产物：{', '.join(outs)}", f"  outputs: {', '.join(outs)}") if outs else ""))
        if a.mark:
            m = r / "DO_NOT_REUSE.md"
            if not m.exists():
                m.write_text(T(f"# 不可复用（继承）\n\n{now_iso()}\n\n来源 {bad} 有错：{a.reason}\n",
                               f"# Do not reuse (inherited)\n\n{now_iso()}\n\nsource {bad} is wrong: {a.reason}\n"), encoding="utf-8")
    print(T("这些运行的数字可能受影响。告诉用户，不要静默修复。", "numbers from these runs may be affected. Tell the user; do not fix silently.")
          if affected else T("没有发现派生运行。", "no derived runs found."))
    return 1 if affected else 0


# ---------------------------------------------------------------- init

def cmd_init(a):
    run = pathlib.Path(a.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    for src, dst in (("gate.template.json", "gate.json"), ("manifest.template.json", "manifest.json"),
                     ("REPORT.template.md", "REPORT.md")):
        if LANG == "en":
            src = src.replace(".template.", ".template.en.")   # 英文模板；zh 和 both 用中文
        if (run / dst).exists():
            print(T(f"已存在，跳过：{dst}", f"exists, skipped: {dst}"))
        else:
            shutil.copyfile(ASSETS / src, run / dst)
            print(T(f"已生成：{dst}", f"created: {dst}"))
    return 0


def fail_list(title, errs):
    print(title + T("：", ":"))
    for e in errs:
        print(f"  - {e}")
    return 1


def main():
    ap = argparse.ArgumentParser(prog="sg", description=T("sim-guard 检查", "sim-guard checks"))
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("init")
    s.add_argument("run_dir")
    s = sub.add_parser("freeze")
    s.add_argument("run_dir")
    s.add_argument("--amend", help=T("改已冻结判据的原因，必须已得到用户同意", "reason for changing frozen criteria; needs user approval"))
    s = sub.add_parser("scan")
    s.add_argument("paths", nargs="+")
    s = sub.add_parser("readback", help=T("读 .mph 或 JSON；--list 列出所有键，--expect 给期望规则", "read .mph or JSON; --list shows all keys, --expect gives rules"))
    s.add_argument("files", nargs="+")
    s.add_argument("--expect", help=T("期望规则的 JSON 文件（列表）；select 的源名写 *", "JSON file with a list of rules; use * as the source name in select"))
    s.add_argument("--list", action="store_true")
    s = sub.add_parser("preflight")
    s.add_argument("run_dir")
    s.add_argument("--script", required=True)
    s.add_argument("--sw")
    s = sub.add_parser("submit", help=T("其余参数原样传给 simq submit，如 --est 3 --after <id>", "other args go to simq submit as is, e.g. --est 3 --after <id>"))
    s.add_argument("run_dir")
    s.add_argument("--script", required=True)
    s.add_argument("--sw", required=True)
    s.add_argument("--name")
    s.add_argument("--simq")
    s = sub.add_parser("report")
    s.add_argument("run_dir")
    s = sub.add_parser("diagnose", help=T("按日志给失败任务分类", "classify a failed job from its log"))
    s.add_argument("job_id")
    s.add_argument("--simq")
    s = sub.add_parser("taint", help=T("标记有错的运行目录，列出所有派生运行", "mark a wrong run directory and list all derived runs"))
    s.add_argument("path")
    s.add_argument("--reason", required=True)
    s.add_argument("--root", help=T("在哪个目录下找派生运行；默认项目根", "where to look for derived runs; default: project root"))
    s.add_argument("--mark", action="store_true", help=T("同时在受影响的运行里写 DO_NOT_REUSE.md", "also write DO_NOT_REUSE.md into affected runs"))

    a, extra = ap.parse_known_args()
    if extra and a.command != "submit":
        ap.error(T(f"未知参数：{extra}", f"unknown arguments: {extra}"))
    try:
        if a.command == "submit":
            return cmd_submit(a, extra)
        return {"init": cmd_init, "freeze": cmd_freeze, "scan": cmd_scan, "readback": cmd_readback,
                "preflight": cmd_preflight, "report": cmd_report, "diagnose": cmd_diagnose,
                "taint": cmd_taint}[a.command](a)
    except UsageError as e:
        print(T(f"错误：{e}", f"error: {e}"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
