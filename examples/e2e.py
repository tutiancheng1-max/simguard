# -*- coding: utf-8 -*-
"""
真实求解器的端到端测试：正确版本要走通，故意设错的版本要被拦下。

  python e2e.py comsol --workdir D:\\e2e --simq C:\\path\\simq.py
  python e2e.py lumerical --workdir D:\\e2e --simq C:\\path\\simq.py

会通过 simq 提交很短的任务，占用求解器 license。--workdir 必须是还不存在的目录。
"""
import argparse
import json
import math
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "sim-guard"
SG = SKILL / "scripts" / "sg.py"
LINE = "simguard-e2e"
CORES = 4
MEM_GB = 4
failures = []


def expect(ok, text):
    print(f"[{'OK' if ok else 'FAIL'}] {text}", flush=True)
    if not ok:
        failures.append(text)


def call(*args):
    return subprocess.run([str(x) for x in args], capture_output=True, text=True, encoding="utf-8", errors="replace")


class Run:
    def __init__(self, a, name, solver, mutate=None):
        self.a, self.solver = a, solver
        self.dir = pathlib.Path(a.workdir) / name
        shutil.copytree(SKILL / "assets" / "templates" / solver, self.dir)
        if mutate:
            mutate(self.dir)
        assert call(sys.executable, SG, "init", self.dir).returncode == 0

    def write(self, gate, readback):
        (self.dir / "gate.json").write_text(json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest = {"run_id": self.dir.name, "line": LINE, "solver": self.solver, "derived_from": [],
                    "drivers": ["build.py", "solve.py", "run_case.py"] +
                               (["CaseDriver.java"] if self.solver == "comsol" else []),
                    "outputs": ["result.json"], "readback": readback, "switch_tests": [], "literature": [],
                    "resources": {"cores": CORES, "mem_gb": MEM_GB, "basis": "minimal template case"},
                    "resume": {"supported": False, "why_not": "runs in minutes"}, "jobs": []}
        (self.dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        assert call(sys.executable, SG, "freeze", self.dir).returncode == 0, "freeze"

    def job(self, script):
        args = [sys.executable, self.a.simq, "submit", "--line", LINE, "--name", f"{self.dir.name}-{script}",
                "--sw", self.solver, "--cores", CORES, "--mem", MEM_GB, "--cwd", self.dir,
                "--script", self.dir / script, "--max-h", 0.5]
        if self.a.smoke_flag:
            args.append("--smoke")
        r = call(*args)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip().splitlines()[-1]

    def wait(self, job_id):
        r = call(sys.executable, self.a.simq, "wait", job_id, "--timeout", 1500)
        return r.returncode == 0, r.stdout.strip()

    def tail(self, job_id):
        return call(sys.executable, self.a.simq, "tail", job_id, "-n", 25).stdout

    def build(self):
        job = self.job("build.py")
        ok, row = self.wait(job)
        if not ok:
            print(self.tail(job))
        expect(ok, f"{self.dir.name}: 建模任务完成 {row}")
        return ok

    def submit(self):
        return call(sys.executable, SG, "submit", self.dir, "--script", "solve.py", "--sw", self.solver,
                    "--simq", self.a.simq, "--max-h", 0.5)

    def preflight(self):
        return call(sys.executable, SG, "preflight", self.dir, "--script", "solve.py", "--sw", self.solver)

    def write_report(self, rows, conv_evidence, validation_evidence, readback_evidence, job_id):
        table = "\n".join(f"| {i} | {v:.6g} | {t} | {c} |" for i, v, t, c in rows)
        overall = "PASS" if all(c == "PASS" for *_, c in rows) else "FAIL"
        (self.dir / "REPORT.md").write_text(
            f"# {self.dir.name}\n\nOverall: {overall}\n\n## 判据结果\n\n| id | 实测值 | 阈值 | 结论 |\n|---|---|---|---|\n{table}\n\n"
            f"## 检查\n\n| check | 状态 | 证据 |\n|---|---|---|\n"
            f"| resolution_convergence | PASS | {conv_evidence} |\n"
            f"| independent_validation | PASS | {validation_evidence} |\n"
            f"| readback | PASS | {readback_evidence} |\n\n"
            f"## 结论和限制\n\n只验证模板算例。\n\n## 偏离计划和按推荐执行的决定\n\n无\n\n## 来源\n\n{job_id}\n",
            encoding="utf-8")
        return call(sys.executable, SG, "report", self.dir)


def validation(description):
    return {"kind": "analytic_limit", "description": description, "shares_code_with_model": False}


# ---------------------------------------------------------------- COMSOL

COMSOL_EXACT = 2 * 1 * 1 / (math.pi ** 3 * 2)
COMSOL_GATE = {"question": "模板算例是否符合解析解",
               "criteria": [{"id": "G1", "metric": "面平均 T 相对误差", "op": "<=", "threshold": 1e-3},
                            {"id": "G2", "metric": "两种网格的相对差", "op": "<=", "threshold": 1e-3}],
               "validation": validation("解析解 2qL^2/(pi^3 k)"), "on_fail": "报告"}
COMSOL_READBACK = {"comsol_tensor_models": ["prepared.mph"]}
GOOD_TENSOR = 'set("c", new String[]{"k", "0", "0", "k"})'


def partial_tensor(allow):
    comment = " // simguard: allow comsol-partial-tensor e2e" if allow else ""

    def mutate(d):
        f = d / "CaseDriver.java"
        text = f.read_text(encoding="utf-8")
        assert GOOD_TENSOR in text
        f.write_text(text.replace(GOOD_TENSOR + ";", 'setIndex("c", "k", 0);' + comment), encoding="utf-8")
    return mutate


def comsol(a):
    good = Run(a, "comsol_good", "comsol")
    good.write(COMSOL_GATE, COMSOL_READBACK)
    if not good.build():
        return
    r = good.submit()
    expect(r.returncode == 0, "comsol_good: 通过 preflight 并提交\n" + r.stdout + r.stderr)
    if r.returncode:
        return
    job = r.stdout.strip().splitlines()[-1]
    ok, row = good.wait(job)
    expect(ok, f"comsol_good: 求解完成 {row}")
    if not ok:
        print(good.tail(job))
        return
    res = json.loads((good.dir / "result.json").read_text(encoding="utf-8"))
    err = abs(res["avg_T_fine"] - COMSOL_EXACT) / COMSOL_EXACT
    conv = abs(res["avg_T_fine"] - res["avg_T_coarse"]) / COMSOL_EXACT
    print(f"    {res}，解析解 {COMSOL_EXACT:.6g}，误差 {err:.2e}，网格差 {conv:.2e}")
    expect(err <= 1e-3 and conv <= 1e-3, "comsol_good: 结果符合解析解")
    r = good.write_report([("G1", err, "<=1e-3", "PASS" if err <= 1e-3 else "FAIL"),
                           ("G2", conv, "<=1e-3", "PASS" if conv <= 1e-3 else "FAIL")],
                          f"网格差 {conv:.2e}", f"解析误差 {err:.2e}", "prepared.mph 张量各向同性", job)
    expect(r.returncode == 0, "comsol_good: 报告合格\n" + r.stdout)

    # 错误版本 1：驱动里有错误写法，不需要求解器就该被拦下
    bad = Run(a, "comsol_bad_scan", "comsol", partial_tensor(allow=False))
    bad.write(COMSOL_GATE, COMSOL_READBACK)
    r = call(sys.executable, SG, "scan", bad.dir / "CaseDriver.java")
    expect(r.returncode == 1 and "comsol-partial-tensor" in r.stdout, "comsol_bad_scan: 扫描查出错误写法")

    # 错误版本 2：扫描被豁免，读回必须独立拦下，且解析校验也要报警
    bad = Run(a, "comsol_bad_readback", "comsol", partial_tensor(allow=True))
    bad.write(COMSOL_GATE, COMSOL_READBACK)
    if not bad.build():
        return
    r = call(sys.executable, SG, "readback", bad.dir / "prepared.mph")
    print("    " + " ".join(re.findall(r'"components": \[[^\]]*\]', r.stdout)).replace("\n", " "))
    expect(r.returncode == 1, "comsol_bad_readback: 读回判定张量错误")
    r = bad.preflight()
    expect(r.returncode == 1 and not (bad.dir / ".simguard" / "receipt.json").exists(), "comsol_bad_readback: preflight 不发回执")
    r = bad.submit()
    expect(r.returncode == 1, "comsol_bad_readback: sg.py submit 拒绝提交")

    # 确认独立校验真的有区分力：不走 sg，直接算一次
    job = bad.job("solve.py")
    ok, row = bad.wait(job)
    if ok:
        res = json.loads((bad.dir / "result.json").read_text(encoding="utf-8"))
        err = abs(res["avg_T_fine"] - COMSOL_EXACT) / COMSOL_EXACT
        print(f"    错误模型 {res}，相对解析解误差 {err:.2e}")
        expect(err > 1e-3, "comsol_bad_readback: 解析校验也能发现这个错误")
    else:
        expect(False, f"comsol_bad_readback: 对照求解失败 {row}")


# ---------------------------------------------------------------- Lumerical

T_EXACT = 0.96
LUM_GATE = {"question": "模板算例是否符合解析解",
            "criteria": [{"id": "G1", "metric": "透射率绝对误差", "op": "<=", "threshold": 5e-3},
                         {"id": "G2", "metric": "两种网格的差", "op": "<=", "threshold": 5e-3}],
            "validation": validation("菲涅耳公式 T=4n/(1+n)^2"), "on_fail": "报告"}
LUM_READBACK = {"dump": "readback.json", "checks": [
    {"path": "slab.index", "op": "approx", "value": 1.5, "rel": 1e-6},
    {"path": "source.wavelength_start_m", "op": "approx", "value": 1.5e-6, "rel": 1e-6},
    {"path": "source.wavelength_stop_m", "op": "approx", "value": 1.6e-6, "rel": 1e-6},
    {"path": "fdtd.x_min_bc", "op": "equals", "value": "Periodic"}]}


def wrong_index(d):
    f = d / "run_case.py"
    text = f.read_text(encoding="utf-8")
    assert '"index", 1.5' in text
    f.write_text(text.replace('"index", 1.5', '"index", 1.4'), encoding="utf-8")


def lumerical(a):
    good = Run(a, "lumerical_good", "lumerical")
    good.write(LUM_GATE, LUM_READBACK)
    if not good.build():
        return
    r = good.submit()
    expect(r.returncode == 0, "lumerical_good: 通过 preflight 并提交\n" + r.stdout + r.stderr)
    if r.returncode:
        return
    job = r.stdout.strip().splitlines()[-1]
    ok, row = good.wait(job)
    expect(ok, f"lumerical_good: 求解完成 {row}")
    if not ok:
        print(good.tail(job))
        return
    res = json.loads((good.dir / "result.json").read_text(encoding="utf-8"))
    err, conv = abs(res["T_fine"] - T_EXACT), abs(res["T_fine"] - res["T_coarse"])
    print(f"    {res}，解析解 {T_EXACT}，误差 {err:.2e}，网格差 {conv:.2e}")
    expect(err <= 5e-3 and conv <= 5e-3, "lumerical_good: 结果符合解析解")
    r = good.write_report([("G1", err, "<=5e-3", "PASS" if err <= 5e-3 else "FAIL"),
                           ("G2", conv, "<=5e-3", "PASS" if conv <= 5e-3 else "FAIL")],
                          f"网格差 {conv:.2e}", f"解析误差 {err:.2e}", "readback.json 四项通过", job)
    expect(r.returncode == 0, "lumerical_good: 报告合格\n" + r.stdout)

    bad = Run(a, "lumerical_bad_index", "lumerical", wrong_index)
    bad.write(LUM_GATE, LUM_READBACK)
    if not bad.build():
        return
    r = bad.preflight()
    expect(r.returncode == 1 and "slab.index" in r.stdout, "lumerical_bad_index: 读回查出折射率错误")

    # lumapi 不能用 --sw python 绕过 license：sg 要拒绝
    r = call(sys.executable, SG, "preflight", good.dir, "--script", "solve.py", "--sw", "python")
    expect(r.returncode == 1, "lumerical_good: --sw python 被拒绝")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("solver", choices=["comsol", "lumerical"])
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--simq", required=True)
    ap.add_argument("--smoke-flag", action="store_true", help="队列开启 require_preflight 时，建模任务加 --smoke")
    a = ap.parse_args()
    if pathlib.Path(a.workdir).exists():
        sys.exit(f"--workdir 已存在：{a.workdir}")
    pathlib.Path(a.workdir).mkdir(parents=True)
    {"comsol": comsol, "lumerical": lumerical}[a.solver](a)
    print(f"\n失败 {len(failures)} 项")
    for f in failures:
        print(" - " + f.splitlines()[0])
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
