# -*- coding: utf-8 -*-
"""
编译 CaseDriver.java 并用 comsolbatch 跑一个阶段。线程数取自 SIMQ_CORES。

comsolbatch 在 Java 出错时退出码仍可能是 0，所以这里另外检查日志和产物，
否则 simq 会把失败的任务记成 done。
"""
import glob
import json
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
DRIVER = "CaseDriver"
EXPECTED = {"build": ["prepared.mph"], "solve": ["solved.mph", "result.json"]}
ERROR_MARK = "/*****Error"


def comsol_bin():
    configured = os.environ.get("COMSOL_BIN")
    if configured:
        return pathlib.Path(configured)
    found = sorted(glob.glob(r"C:\Program Files\COMSOL\COMSOL*\Multiphysics\bin\win64"))
    if not found:
        sys.exit("找不到 COMSOL：请设置环境变量 COMSOL_BIN")
    return pathlib.Path(found[-1])


def compile_java(bin_dir, stage):
    run_dir = HERE.as_posix()
    params_file = HERE / "params.json"
    params = json.loads(params_file.read_text(encoding="utf-8-sig")) if params_file.exists() else {}
    rows = ", ".join(f"{{{json.dumps(str(k))}, {json.dumps(str(v))}}}" for k, v in params.items())
    (HERE / "CaseConfig.java").write_text(
        f'public class CaseConfig {{\n  static final String RUN_DIR = "{run_dir}";\n'
        f'  static final String STAGE = "{stage}";\n'
        f'  static final String[][] PARAMS = {{{rows}}};\n}}\n', encoding="utf-8")
    for name in ("CaseConfig", DRIVER):
        code = subprocess.call([str(bin_dir / "comsolcompile.exe"), "-classpathadd", str(HERE),
                                str(HERE / f"{name}.java")])
        if code or not (HERE / f"{name}.class").exists():
            sys.exit(f"{name} 编译失败，退出码 {code}")


def main(stage):
    bin_dir = comsol_bin()
    work = HERE / "work"
    (work / "native").mkdir(parents=True, exist_ok=True)
    (work / "recovery").mkdir(parents=True, exist_ok=True)
    for rel in EXPECTED[stage]:
        (HERE / rel).unlink(missing_ok=True)    # 不让上一次的产物冒充这一次的结果
    compile_java(bin_dir, stage)
    log = work / f"{stage}.log"
    proc = subprocess.Popen([str(bin_dir / "comsolbatch.exe"), "-np", os.environ.get("SIMQ_CORES", "2"),
                             "-tmpdir", str(work / "native"), "-recoverydir", str(work / "recovery"),
                             "-inputfile", str(HERE / f"{DRIVER}.class"), "-classpathadd", str(HERE),
                             "-batchlog", str(log), "-batchlogout", "-nosave"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    lines = []
    for line in proc.stdout:    # 程序里的 println 只出现在标准输出，不在 -batchlog 里
        print(line, end="", flush=True)
        lines.append(line)
    code = proc.wait()
    text = "".join(lines)
    if log.exists():
        text += log.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^SG_RESULT\|(.*)$", text, re.M)
    if match:
        (HERE / "result.json").write_text(match[1] + "\n", encoding="utf-8")
    if code:
        sys.exit(f"comsolbatch 退出码 {code}")
    if ERROR_MARK in text:
        sys.exit(f"COMSOL 日志里有错误，见 {log}")
    missing = [rel for rel in EXPECTED[stage] if not (HERE / rel).exists()]
    if missing:
        sys.exit(f"阶段 {stage} 缺少产物：{missing}")
