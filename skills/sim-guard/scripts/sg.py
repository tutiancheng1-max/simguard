# -*- coding: utf-8 -*-
"""
sg.py —— sim-guard 的检查脚本，只依赖 Python 标准库。

  python sg.py init <run_dir>                     生成 gate.json、manifest.json、REPORT.md 模板
  python sg.py freeze <run_dir>                   检查并冻结判据
  python sg.py freeze <run_dir> --amend "原因"    经用户同意后改判据，留下记录
  python sg.py scan <路径...>                     搜索已知的错误写法
  python sg.py readback <model.mph...>            直接读已保存的 COMSOL 模型，核对张量
  python sg.py preflight <run_dir> --script x     全部提交前检查，通过后写回执
  python sg.py submit <run_dir> --script x --sw comsol --name N [其他 simq 参数]
  python sg.py report <run_dir>                   检查报告并记录来源哈希
  python sg.py diagnose <job_id>                  按日志给失败任务分类

退出码：0 = 通过，1 = 检查不通过，2 = 用法或配置错误。
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"
PROJECT_FILE = "simguard.project.json"
PLACEHOLDER = "TODO"

VALIDATION_KINDS = ("analytic_limit", "conservation", "independent_method", "literature_reproduction", "experiment")
GATE_OPS = ("<", "<=", ">", ">=", "==", "within")
REPORT_OVERALL = ("PASS", "PARTIAL", "FAIL", "BLOCKED")
REPORT_CHECK = ("PASS", "FAIL", "NOT_DONE", "NA")
REQUIRED_CHECKS = ("resolution_convergence", "independent_validation", "readback")
MODEL_SOLVERS = ("comsol", "lumerical", "fluent")   # 这些求解器有保存的模型，必须读回

# 只放跨项目都成立的规则；项目自己的规则写进 simguard.project.json。
BUILTIN_PATTERNS = [
    {"id": "comsol-partial-tensor", "glob": "*.java", "regex": r'\.setIndex\(\s*"c"\s*,',
     "why": "setIndex 只设置张量的一个分量，其余分量保留默认值 1；要用 set(\"c\", 完整张量)"},
]
BUILTIN_MARKERS = ["DO_NOT_REUSE*"]
TEXT_EXT = {".java", ".py", ".m", ".ps1", ".bat", ".cmd", ".lsf", ".jou", ".scm", ".txt"}

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
        raise UsageError(f"{path} 不是合法 JSON：{e}")


def write_json(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
        errs.append(f"gate.json 还有 {PLACEHOLDER} 没填")
    if not gate.get("question"):
        errs.append("缺少 question")
    crit = gate.get("criteria") or []
    if not crit:
        errs.append("至少要有一条 criteria")
    for c in crit:
        for k in ("id", "metric", "op", "threshold"):
            if k not in c:
                errs.append(f"criteria 缺少 {k}：{c}")
        if c.get("op") not in GATE_OPS:
            errs.append(f"op 只能是 {GATE_OPS}：{c.get('id')}")
    v = gate.get("validation") or {}
    if v.get("kind") not in VALIDATION_KINDS:
        errs.append(f"validation.kind 只能是 {VALIDATION_KINDS}；和旧结果一致不算独立校验")
    if not v.get("description"):
        errs.append("validation.description 要写清校验方法")
    if v.get("shares_code_with_model") is not False:
        errs.append("validation.shares_code_with_model 必须是 false：校验不能和被检模型共用同一段可能出错的设置")
    return errs


def gate_lock_errors(run):
    gate_f, lock_f = run / "gate.json", run / "gate.lock.json"
    if not gate_f.is_file():
        return ["缺少 gate.json"], None
    if not lock_f.is_file():
        return ["判据没有冻结：先跑 sg.py freeze"], None
    sha = sha256_file(gate_f)
    lock = read_json(lock_f)
    if lock.get("sha256") != sha:
        return ["gate.json 在冻结后改过；改判据要经用户同意，再用 freeze --amend 记录"], sha
    return [], sha


def cmd_freeze(a):
    run = pathlib.Path(a.run_dir)
    gate_f, lock_f = run / "gate.json", run / "gate.lock.json"
    if not gate_f.is_file():
        raise UsageError(f"找不到 {gate_f}")
    errs = gate_errors(read_json(gate_f))
    if errs:
        return fail_list("判据不合格", errs)
    sha = sha256_file(gate_f)
    history = []
    if lock_f.is_file():
        lock = read_json(lock_f)
        if lock.get("sha256") == sha:
            print(f"判据已冻结，未变化 sha256={sha[:12]}")
            return 0
        if not a.amend:
            return fail_list("判据已冻结", ["内容已变化；只有用户同意后才能用 --amend \"原因\" 更新"])
        history = lock.get("history", []) + [{"sha256": lock["sha256"], "frozen_at": lock.get("frozen_at"),
                                              "replaced_at": now_iso(), "reason": a.amend}]
    write_json(lock_f, {"sha256": sha, "frozen_at": now_iso(), "history": history})
    print(f"已冻结判据 sha256={sha[:12]}")
    return 0


# ---------------------------------------------------------------- 错误写法扫描

def iter_files(paths):
    for p in paths:
        p = pathlib.Path(p)
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and f.suffix.lower() in TEXT_EXT:
                    yield f
        elif p.is_file():
            yield p


def scan_paths(paths, project):
    """返回 [(文件, 行号, 规则 id, 原因)]；行内 simguard: allow <id> 可以豁免一条。"""
    hits = []
    for f in iter_files(paths):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for rule in forbidden_patterns(project):
            if not fnmatch.fnmatch(f.name, rule.get("glob", "*")):
                continue
            rx = re.compile(rule["regex"])
            for no, line in enumerate(text.splitlines(), 1):
                if rx.search(line) and f"simguard: allow {rule['id']}" not in line:
                    hits.append((str(f), no, rule["id"], rule.get("why", "")))
    return hits


def cmd_scan(a):
    hits = scan_paths(a.paths, load_project(a.paths[0]))
    for f, no, rid, why in hits:
        print(f"{f}:{no} [{rid}] {why}")
    print(f"共 {len(hits)} 处")
    return 1 if hits else 0


# ---------------------------------------------------------------- COMSOL 读回：直接读 .mph 里的 dmodel.xml

ZERO = re.compile(r"[+-]?0(?:\.0*)?(?:\[[^\]]+\])?")
DEFAULT_TENSOR_TAGS = ("tte", "ttl", "tts")
DEFAULT_TENSOR_FIELDS = ("T", "Te", "Tl")


def check_comsol_tensors(path, tags=DEFAULT_TENSOR_TAGS, fields=DEFAULT_TENSOR_FIELDS, anisotropic_ok=()):
    """核对扩散系数 c 是否各向同性。只读模型文件，不需要 COMSOL，也不占 license。"""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("dmodel.xml"))
    ok_keys = {(x.get("physics"), x.get("feature")) for x in anisotropic_ok}
    rows = []
    for phys in root.iter("Physics"):
        names = [x.text or "" for x in phys.iter() if x.tag == "fieldname"]
        target = phys.get("tag") in tags or (phys.get("op") == "CoefficientFormPDE" and any(n in fields for n in names))
        if not target:
            continue
        for feat in phys.iter("PhysicsFeature"):
            for par in feat.findall("param"):
                if par.get("param") != "c":
                    continue
                rows.append(_tensor_row(phys.get("tag"), feat.get("tag"), par.get("value", ""), ok_keys))
    status = "PASS" if rows and all(r["status"] == "PASS" for r in rows) else "FAIL"
    return {"path": str(pathlib.Path(path).resolve()), "status": status, "tensors": rows,
            "note": "" if rows else "没有找到目标张量：可能是物理场标签不对，不代表模型没问题"}


def _tensor_row(physics, feature, raw, ok_keys):
    m = re.fullmatch(r"(\d+)\|(\d+),'(.*)'", raw, re.S)
    values = m[3].split("','") if m else []
    norm = [re.sub(r"\s+", "", v.replace("\\,", ",")) for v in values]
    n = {1: 1, 4: 2, 9: 3}.get(len(norm), 0)
    is_zero = lambda s: bool(ZERO.fullmatch(s))
    if n:
        diag = [norm[i * (n + 1)] for i in range(n)]
        off = [v for i, v in enumerate(norm) if i // n != i % n]
        passed = len(set(diag)) == 1 and not is_zero(diag[0]) and all(is_zero(v) for v in off)
    else:
        passed = False
    allowed = (physics, feature) in ok_keys
    return {"physics": physics, "feature": feature, "components": values, "dimension": n,
            "status": "PASS" if passed or allowed else "FAIL",
            "anisotropic_declared": allowed and not passed}


def same_value(got, want):
    try:
        return float(got) == float(want)
    except (TypeError, ValueError):
        return re.sub(r"\s+", "", str(got)) == re.sub(r"\s+", "", str(want))


def comsol_param_errors(path, expected):
    """核对模型里保存的全局参数，确认扫描值真的写进了模型。"""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("dmodel.xml"))
    saved = {}
    for e in root.iter("expressions"):
        saved.setdefault(e.get("name"), e.get("expr"))
    errs = []
    for name, want in expected.items():
        if name not in saved:
            errs.append(f"模型里没有参数 {name}")
        elif not same_value(saved[name], want):
            errs.append(f"参数 {name}：模型里是 {saved[name]}，期望 {want}")
    return errs


def cmd_readback(a):
    result = [check_comsol_tensors(p) for p in a.models]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(r["status"] == "PASS" for r in result) else 1


# ---------------------------------------------------------------- 其他求解器的读回：比较求解器导出的 JSON

def dig(data, dotted):
    for key in dotted.split("."):
        if isinstance(data, list):
            data = data[int(key)]
        else:
            data = data[key]
    return data


def check_dump(dump, checks):
    errs = []
    for c in checks:
        path, op = c.get("path"), c.get("op")
        try:
            got = dig(dump, path)
        except (KeyError, IndexError, ValueError, TypeError):
            errs.append(f"读回里没有 {path}")
            continue
        if op == "equals":
            good = got == c.get("value")
        elif op == "approx":
            want = float(c["value"])
            good = abs(float(got) - want) <= float(c.get("rel", 1e-6)) * max(abs(want), 1e-300)
        elif op == "isotropic":
            good = isinstance(got, list) and len(got) > 0 and all(x == got[0] for x in got)
        else:
            errs.append(f"未知比较方式 {op}：{path}")
            continue
        if not good:
            errs.append(f"{path}：读回 {got!r}，期望 {op} {c.get('value', '')}")
    return errs


def readback_errors(run, manifest, project):
    rb = manifest.get("readback")
    if not isinstance(rb, dict):
        return ["manifest.readback 要写明读回方式；确实不需要时写 {\"not_applicable\": \"原因\"}"]
    if rb.get("not_applicable"):
        if manifest.get("solver") in MODEL_SOLVERS:
            return [f"{manifest['solver']} 模型不能跳过读回：用 comsol_tensor_models，或导出 dump 并配 checks"]
        return []
    errs = []
    conf = project.get("comsol_tensor", {})
    tags = tuple(conf.get("physics_tags", DEFAULT_TENSOR_TAGS))
    fields = tuple(conf.get("fields", DEFAULT_TENSOR_FIELDS))
    comsol = rb.get("comsol_tensor_models") or []
    for m in comsol:
        p = run / m
        if not p.is_file():
            errs.append(f"读回模型不存在：{m}")
            continue
        try:
            res = check_comsol_tensors(p, tags, fields, rb.get("anisotropic_ok") or [])
        except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
            errs.append(f"{m} 读不出 dmodel.xml：{e}")
            continue
        if res["status"] != "PASS":
            bad = [r for r in res["tensors"] if r["status"] != "PASS"]
            detail = "；".join(f"{r['physics']}/{r['feature']} c={r['components']}" for r in bad) or res["note"]
            errs.append(f"{m} 张量读回不通过：{detail}")
    params = rb.get("comsol_params") or {}
    if params:
        if not comsol:
            errs.append("comsol_params 要配合 comsol_tensor_models 使用")
        elif (run / comsol[0]).is_file():
            errs += comsol_param_errors(run / comsol[0], params)
    dump = rb.get("dump")
    if dump:
        p = run / dump
        if not p.is_file():
            errs.append(f"读回文件不存在：{dump}")
        else:
            errs += check_dump(read_json(p), rb.get("checks") or [])
            if not rb.get("checks"):
                errs.append("readback.dump 没有配 checks，读回等于没查")
    if not comsol and not dump:
        errs.append("manifest.readback 既没有模型也没有读回文件")
    return errs


# ---------------------------------------------------------------- preflight

def provenance_errors(run, manifest, project, script):
    errs = []
    approved = project_paths(project, "approved_templates")
    derived = [run / d for d in manifest.get("derived_from") or []]
    for src in derived:
        if not src.exists():
            errs.append(f"derived_from 不存在：{src}")
            continue
        src_dir = src if src.is_dir() else src.parent
        for marker in reuse_markers(project):
            found = list(src_dir.glob(marker))
            if found:
                errs.append(f"{src} 所在目录标记了不可复用：{found[0].name}")
                break
        if approved and not any(src.resolve() == t or is_within(src, t) for t in approved):
            errs.append(f"{src} 不在 approved_templates 里")
    drivers = [script] + [run / d for d in manifest.get("drivers") or []]
    for d in drivers:
        if not pathlib.Path(d).exists():
            errs.append(f"驱动脚本不存在：{d}")
    for f, no, rid, why in scan_paths([d for d in drivers if pathlib.Path(d).exists()], project):
        errs.append(f"{f}:{no} [{rid}] {why}")
    return errs


def scope_errors(run, project):
    errs = []
    for ro in project_paths(project, "readonly_paths"):
        if is_within(run, ro):
            errs.append(f"run 目录在只读范围内：{ro}")
    return errs


def status_list_errors(items, label):
    errs = []
    for item in items or []:
        if item.get("status") != "PASS":
            errs.append(f"{label}未通过：{item}")
    return errs


def manifest_errors(manifest):
    errs = []
    if has_placeholder(manifest):
        errs.append(f"manifest.json 还有 {PLACEHOLDER} 没填")
    for k in ("run_id", "line", "solver"):
        if not manifest.get(k):
            errs.append(f"manifest 缺少 {k}")
    res = manifest.get("resources") or {}
    for k in ("cores", "mem_gb", "basis"):
        if not res.get(k):
            errs.append(f"manifest.resources 缺少 {k}；basis 写内存估算依据，例如小测试实测")
    resume = manifest.get("resume") or {}
    if "supported" not in resume or (resume.get("supported") and not resume.get("how")) or \
            (resume.get("supported") is False and not resume.get("why_not")):
        errs.append("manifest.resume 要写 supported，并给出 how 或 why_not")
    return errs


def preflight(run, script, sw=None):
    """返回 ([(检查项, [错误])], gate 哈希)。"""
    if not (run / "manifest.json").is_file():
        return [("manifest", ["缺少 manifest.json：先跑 sg.py init"])], None
    manifest = read_json(run / "manifest.json")
    project = load_project(run)
    lock_errs, gate_sha = gate_lock_errors(run)
    gate = run / "gate.json"
    gate_errs = gate_errors(read_json(gate)) if gate.is_file() else []
    solver_errs = []
    sources = [script] + [run / d for d in manifest.get("drivers") or []]
    if manifest.get("solver") != "lumerical":
        for src in sources:
            if src.is_file() and src.suffix.lower() == ".py" and \
                    re.search(r"^\s*(import|from)\s+lumapi\b", src.read_text(encoding="utf-8", errors="replace"), re.M):
                solver_errs.append(f"{src.name} 调用 lumapi，manifest.solver 必须是 lumerical，否则绕过 license 排队")
    if sw and manifest.get("solver") and sw != manifest["solver"]:
        solver_errs.append(f"--sw {sw} 和 manifest.solver {manifest['solver']} 不一致")
    return [
        ("清单完整", manifest_errors(manifest)),
        ("工作范围", scope_errors(run, project)),
        ("判据冻结", lock_errs + gate_errs),
        ("求解器类型", solver_errs),
        ("驱动来源和错误写法", provenance_errors(run, manifest, project, script)),
        ("模型读回", readback_errors(run, manifest, project)),
        ("开关测试", status_list_errors(manifest.get("switch_tests"), "开关测试")),
        ("文献公式复现", status_list_errors(manifest.get("literature"), "文献复现")),
    ], gate_sha


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
        write_json(receipt, {"status": "PASS", "created": now_iso(), "script": str(script.resolve()),
                             "script_sha256": sha256_file(script), "gate_sha256": gate_sha})
        print(f"通过，回执：{receipt}")
    else:
        if receipt.is_file():
            receipt.unlink()   # 旧回执不再有效
        print("不通过，不能提交。修复问题，不要绕过检查。")
    return passed


def resolve_script(run, script):
    """相对路径按 run 目录解析，与 manifest 里的路径一致。"""
    p = pathlib.Path(script)
    return p if p.is_absolute() else pathlib.Path(run) / p


def cmd_preflight(a):
    run = pathlib.Path(a.run_dir).resolve()
    return 0 if run_preflight(run, resolve_script(run, a.script), a.sw) else 1


def find_simq(a, project):
    candidates = [a.simq, os.environ.get("SIMQ_PY")]
    if project.get("simq_py") and project.get("_root"):
        candidates.append(str(pathlib.Path(project["_root"], project["simq_py"])))
    for c in candidates:
        if c and pathlib.Path(c).is_file():
            return c
    raise UsageError("找不到 simq.py：用 --simq、环境变量 SIMQ_PY 或项目配置 simq_py 指定。不要改为直接启动求解器。")


def cmd_submit(a, extra):
    run = pathlib.Path(a.run_dir).resolve()
    script = resolve_script(run, a.script).resolve()
    simq = find_simq(a, load_project(run))
    if not run_preflight(run, script, a.sw):
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
    job_id = done.stdout.strip().splitlines()[-1]
    manifest["jobs"] = (manifest.get("jobs") or []) + [job_id]
    write_json(run / "manifest.json", manifest)
    return 0


# ---------------------------------------------------------------- 失败分类

# 按顺序匹配。只有 license 这类暂时性问题适合自动重试；其他原因重试只会再失败一次。
FAILURE_CLASSES = [
    ("memory", r"out of memory|outofmemory|memory allocation|insufficient memory|内存不足|超过声明内存"),
    ("license", r"licen[sc]e.*(fail|error|unavailable|denied|could not|not available|checkout)"
                r"|(fail|could not|unable|cannot).*licen[sc]e|license 占满"),
    ("convergence", r"not converge|failed to find a solution|diverge|发散|nan detected"),
    ("config", r"error running java|traceback|syntaxerror|no such file|cannot find|not found|编译失败|不存在"),
]
FAILURE_ADVICE = {
    "memory": "减少线程或模型规模，并更新内存估计；不要原样重试",
    "license": "license 暂时不可用，可以重试",
    "convergence": "先诊断物理和数值设置；同一问题最多重试 2 次",
    "config": "修复脚本或配置；不要原样重试",
    "unknown": "读日志诊断",
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
        raise UsageError(done.stderr.strip() or "读不到日志")
    name, evidence = classify_failure(done.stdout)
    print(f"类别：{name}\n证据：{evidence or '无'}\n建议：{FAILURE_ADVICE[name]}")
    return 0


# ---------------------------------------------------------------- 报告

def parse_report(text):
    """取 Overall 状态和检查表。表格行格式：| check | 状态 | 证据 |"""
    m = re.search(r"^Overall:\s*(\S+)", text, re.M)
    overall = m[1] if m else None
    checks = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.lstrip().startswith("|") and len(cells) >= 3 and cells[1] in REPORT_CHECK:
            checks[cells[0]] = (cells[1], cells[2])
    return overall, checks


def report_errors(run, project):
    f = run / "REPORT.md"
    if not f.is_file():
        return ["缺少 REPORT.md"]
    text = f.read_text(encoding="utf-8-sig")
    errs = []
    if PLACEHOLDER in text:
        errs.append(f"REPORT.md 还有 {PLACEHOLDER} 没填；按模板填写所有段落")
    overall, checks = parse_report(text)
    if overall not in REPORT_OVERALL:
        errs.append(f"Overall 只能是 {REPORT_OVERALL}")
    required = list(REQUIRED_CHECKS) + list(project.get("required_report_checks", []))
    for name in required:
        if name not in checks:
            errs.append(f"检查表缺少 {name}")
    for name, (status, evidence) in checks.items():
        if status in ("PASS", "NA") and not evidence:
            errs.append(f"{name} 标了 {status}，但没写证据或原因")
    bad = [n for n in required if checks.get(n, ("",))[0] in ("NOT_DONE", "FAIL")]
    if overall == "PASS" and bad:
        errs.append(f"Overall 不能写 PASS：{bad} 没做或没通过；应写 PARTIAL 或 FAIL")
    gate_f = run / "gate.json"
    if gate_f.is_file():
        for c in read_json(gate_f).get("criteria", []):
            if str(c.get("id")) not in text:
                errs.append(f"“判据结果”表里缺少判据 {c.get('id')}：每条判据一行，写 id、实测值、阈值和结论")
    return errs


def cmd_report(a):
    run = pathlib.Path(a.run_dir)
    errs = report_errors(run, load_project(run))
    lock_errs, gate_sha = gate_lock_errors(run)
    errs += lock_errs
    if errs:
        return fail_list("报告不合格", errs)
    manifest = read_json(run / "manifest.json")
    if not manifest.get("jobs"):
        print("提醒：manifest.jobs 里没有 job id")
    files = {}
    for rel in (manifest.get("drivers") or []) + (manifest.get("outputs") or []) + \
            ((manifest.get("readback") or {}).get("comsol_tensor_models") or []):
        p = run / rel
        files[rel] = sha256_file(p) if p.is_file() else None
    missing = [k for k, v in files.items() if v is None]
    write_json(run / "provenance.json", {"created": now_iso(), "gate_sha256": gate_sha,
                                         "jobs": manifest.get("jobs") or [], "files": files})
    if missing:
        return fail_list("来源文件缺失", missing)
    print("报告合格，已写 provenance.json")
    return 0


# ---------------------------------------------------------------- init

def cmd_init(a):
    run = pathlib.Path(a.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    for src, dst in (("gate.template.json", "gate.json"), ("manifest.template.json", "manifest.json"),
                     ("REPORT.template.md", "REPORT.md")):
        if (run / dst).exists():
            print(f"已存在，跳过：{dst}")
        else:
            shutil.copyfile(ASSETS / src, run / dst)
            print(f"已生成：{dst}")
    return 0


def fail_list(title, errs):
    print(title + "：")
    for e in errs:
        print(f"  - {e}")
    return 1


def main():
    ap = argparse.ArgumentParser(prog="sg", description="sim-guard 检查")
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("init")
    s.add_argument("run_dir")
    s = sub.add_parser("freeze")
    s.add_argument("run_dir")
    s.add_argument("--amend", help="改已冻结判据的原因，必须已得到用户同意")
    s = sub.add_parser("scan")
    s.add_argument("paths", nargs="+")
    s = sub.add_parser("readback")
    s.add_argument("models", nargs="+")
    s = sub.add_parser("preflight")
    s.add_argument("run_dir")
    s.add_argument("--script", required=True)
    s.add_argument("--sw")
    s = sub.add_parser("submit", help="其余参数原样传给 simq submit，如 --est 3 --after <id>")
    s.add_argument("run_dir")
    s.add_argument("--script", required=True)
    s.add_argument("--sw", required=True)
    s.add_argument("--name")
    s.add_argument("--simq")
    s = sub.add_parser("report")
    s.add_argument("run_dir")
    s = sub.add_parser("diagnose", help="按日志给失败任务分类")
    s.add_argument("job_id")
    s.add_argument("--simq")

    a, extra = ap.parse_known_args()
    if extra and a.command != "submit":
        ap.error(f"未知参数：{extra}")
    try:
        if a.command == "submit":
            return cmd_submit(a, extra)
        return {"init": cmd_init, "freeze": cmd_freeze, "scan": cmd_scan, "readback": cmd_readback,
                "preflight": cmd_preflight, "report": cmd_report, "diagnose": cmd_diagnose}[a.command](a)
    except UsageError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
