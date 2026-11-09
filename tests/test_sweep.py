# -*- coding: utf-8 -*-
"""参数扫描和监控的测试。只跑纯 Python 假任务（--sw other），不调用求解器，也不碰已有队列。"""
import csv
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "sim-guard" / "scripts"
SWEEP = SCRIPTS / "sweep.py"
SG = SCRIPTS / "sg.py"
SIMQ = ROOT / "simq" / "simq.py"

BUILD = """
import json
p = json.load(open("params.json"))
k = p["k"] if p.get("mode") != "wrong_param" else -1
json.dump({"k": k}, open("readback.json", "w"))
open("prepared.txt", "w").write("ok")
"""
SOLVE = """
import json, sys
p = json.load(open("params.json"))
mode = p.get("mode")
if mode == "oom":
    print("Out of memory during factorization"); sys.exit(1)
if mode == "license":
    import os
    if not os.path.exists("tried"):
        open("tried", "w").write("1")
        print("Could not obtain license"); sys.exit(1)
if mode == "marker":
    print("/*****Error"); sys.exit(0)
if mode == "false_done":
    sys.exit(0)
if mode == "nan":
    json.dump({"value": float("nan")}, open("result.json", "w")); sys.exit(0)
json.dump({"value": p["k"] * 2, "note": "x"}, open("result.json", "w"))
"""


class Queue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.home = self.dir / "home"
        self.home.mkdir()
        self.config = {"cores": 8, "mem_gb": 8, "min_free_mem_gb": 0, "licenses": {}, "poll_s": 1,
                       "require_preflight": True}
        (self.home / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        self.env = dict(os.environ, SIMQ_HOME=str(self.home), SIMQ_PY=str(SIMQ))
        self.addCleanup(self.stop)

    def stop(self):
        subprocess.run([sys.executable, str(SIMQ), "dispatcher", "stop"], env=self.env, capture_output=True)
        for _ in range(30):
            if not (self.home / "DISPATCHER_STOP").exists():
                break
            time.sleep(0.5)
        time.sleep(1.5)

    def call(self, script, *args):
        return subprocess.run([sys.executable, str(script), *map(str, args)], env=self.env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")


class Sweep(Queue):
    def setUp(self):
        super().setUp()
        self.base = self.dir / "sweep"
        tpl = self.base / "tpl"
        tpl.mkdir(parents=True)
        (tpl / "build.py").write_text(BUILD, encoding="utf-8")
        (tpl / "solve.py").write_text(SOLVE, encoding="utf-8")
        gate = {"question": "q", "criteria": [{"id": "G1", "metric": "v", "op": ">=", "threshold": 3,
                                               "result_key": "value"}],
                "validation": {"kind": "analytic_limit", "description": "x", "shares_code_with_model": False},
                "on_fail": "report"}
        manifest = {"derived_from": [], "drivers": ["build.py"], "outputs": ["result.json"],
                    "readback": {"dump": "readback.json", "checks": [
                        {"path": "k", "op": "approx", "value": {"$param": "k"}}]},
                    "switch_tests": [], "literature": [],
                    "resources": {"cores": 1, "mem_gb": 1, "basis": "test"},
                    "resume": {"supported": False, "why_not": "test"}}
        (tpl / "gate.json").write_text(json.dumps(gate), encoding="utf-8")
        (tpl / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.spec = {"name": "s", "line": "t", "solver": "other", "template": "tpl", "max_retry": 1,
                     "stages": [
                         {"name": "build", "script": "build.py", "cores": 1, "mem_gb": 1, "max_h": 0.1,
                          "outputs": ["prepared.txt", "readback.json"]},
                         {"name": "solve", "script": "solve.py", "cores": 1, "mem_gb": 1, "guarded": True,
                          "outputs": ["result.json"]}]}
        self.file = self.base / "sweep.json"

    def plan(self, params, **extra):
        self.file.write_text(json.dumps(dict(self.spec, params=params, **extra)), encoding="utf-8")
        return self.call(SWEEP, "plan", self.file)

    def state(self):
        return json.loads((self.base / "sweep_state.json").read_text(encoding="utf-8"))

    def stage(self, mode):
        for case in self.state()["cases"].values():
            if case["params"].get("mode") == mode:
                return case["stages"]

    def run_modes(self, *modes):
        r = self.plan({"k": [1] * len(modes), "mode": list(modes)}, mode="zip")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return self.call(SWEEP, "advance", self.file, "--wait", 120)

    def test_grid_flow_collect_and_no_rerun(self):
        r = self.plan({"k": [1, 2], "x": [10, 20]})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("算例 4 个", r.stdout)
        r = self.call(SWEEP, "advance", self.file, "--wait", 120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        jobs = sorted((self.home / "jobs").iterdir())
        self.assertEqual(len(jobs), 8)
        self.assertEqual(self.call(SWEEP, "advance", self.file).returncode, 0)
        self.assertEqual(len(list((self.home / "jobs").iterdir())), 8)

        self.assertEqual(self.call(SWEEP, "collect", self.file).returncode, 0)
        with open(self.base / "sweep_results.csv", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        verdicts = {(r["param:k"], r["gate:G1"]) for r in rows}
        self.assertEqual(verdicts, {("1", "FAIL"), ("2", "PASS")})
        # 提交记录也要写进 manifest，正式求解有回执
        run = next((self.base / "cases").iterdir())
        self.assertEqual(len(json.loads((run / "manifest.json").read_text(encoding="utf-8"))["jobs"]), 1)
        job = [d for d in jobs if json.loads((d / "job.json").read_text(encoding="utf-8"))["name"].endswith("solve")][0]
        self.assertIn("script_sha256", json.loads((job / "job.json").read_text(encoding="utf-8"))["preflight"])

    def test_wrong_param_blocked_by_readback(self):
        r = self.run_modes("wrong_param")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        rec = self.stage("wrong_param")["solve"]
        self.assertEqual(rec["cause"], "preflight")
        self.assertIn("k：读回", " ".join(rec["detail"]))

    def test_failure_policy(self):
        r = self.run_modes("oom", "license", "marker", "false_done", "nan", "ok")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        oom = self.stage("oom")["solve"]
        self.assertEqual((oom["cause"], oom["attempts"]), ("memory", 1))     # 不重试
        lic = self.stage("license")["solve"]
        self.assertEqual((lic["status"], lic["attempts"]), ("done", 2))      # 重试一次后成功
        self.assertEqual(self.stage("false_done")["solve"]["cause"], "false_done")
        nan = self.stage("nan")["solve"]
        self.assertEqual(nan["cause"], "false_done")                         # 退出码 0，但结果是 NaN
        self.assertIn("NaN", " ".join(nan["detail"]))
        self.assertEqual(self.stage("marker")["solve"]["status"], "needs_attention")
        self.assertEqual(self.stage("ok")["solve"]["status"], "done")

    def test_template_change_refused(self):
        self.assertEqual(self.plan({"k": [1]}).returncode, 0)
        (self.base / "tpl" / "build.py").write_text("# changed\n" + BUILD, encoding="utf-8")
        self.assertEqual(self.plan({"k": [1, 2]}).returncode, 1)

    def test_template_test_run_leftovers_ignored(self):
        self.assertEqual(self.plan({"k": [1]}).returncode, 0)
        (self.base / "tpl" / "work").mkdir()
        (self.base / "tpl" / "work" / "native.tmp").write_text("x", encoding="utf-8")
        (self.base / "tpl" / "CaseDriver.class").write_bytes(b"\xca\xfe")
        r = self.plan({"k": [1, 2]})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_half_made_case_cleaned_up(self):
        self.assertEqual(self.plan({"k": [1]}).returncode, 0)
        sys.path.insert(0, str(SCRIPTS))
        import sweep
        orphan = self.base / "cases" / sweep.case_id({"k": 2})
        orphan.mkdir()
        (orphan / "junk.txt").write_text("x", encoding="utf-8")
        r = self.plan({"k": [1, 2]})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((orphan / "junk.txt").exists())

    def test_build_stage_needs_max_h(self):
        del self.spec["stages"][0]["max_h"]
        r = self.plan({"k": [1]})
        self.assertEqual(r.returncode, 2)
        self.assertIn("max_h", r.stderr)

    def test_broken_case_does_not_stop_others(self):
        self.assertEqual(self.plan({"k": [1, 2]}).returncode, 0)
        cases = sorted((self.base / "cases").iterdir())
        (cases[0] / "manifest.json").write_text("{broken", encoding="utf-8")
        r = self.call(SWEEP, "advance", self.file, "--wait", 120)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        state = self.state()["cases"]
        self.assertEqual(state[cases[0].name]["stages"]["solve"]["cause"], "error")
        self.assertEqual(state[cases[1].name]["stages"]["solve"]["status"], "done")
        # 每个提交过的任务只提交一次
        names = [json.loads((d / "job.json").read_text(encoding="utf-8"))["name"] for d in (self.home / "jobs").iterdir()]
        self.assertEqual(len(names), len(set(names)))

    def test_collect_flags_degenerate_and_tampered_gate(self):
        r = self.plan({"k": [1], "x": [10, 20]})          # x 没有用到：两个算例结果完全相同
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.call(SWEEP, "advance", self.file, "--wait", 120).returncode, 0)
        r = self.call(SWEEP, "collect", self.file)
        self.assertEqual(r.returncode, 1)
        self.assertIn("结果完全相同", r.stdout)
        run = next((self.base / "cases").iterdir())
        gate = json.loads((run / "gate.json").read_text(encoding="utf-8"))
        gate["criteria"][0]["threshold"] = 0                  # 运行后放宽阈值
        (run / "gate.json").write_text(json.dumps(gate), encoding="utf-8")
        self.call(SWEEP, "collect", self.file)
        with open(self.base / "sweep_results.csv", encoding="utf-8-sig") as f:
            rows = {r["case"]: r for r in csv.DictReader(f)}
        self.assertEqual(rows[run.name]["gate:G1"], "BLOCKED")

    def test_case_limit(self):
        r = self.plan({"k": list(range(5))}, max_cases=3)
        self.assertEqual(r.returncode, 2)
        self.assertIn("max_cases", r.stderr)

    def test_stage_needs_outputs(self):
        del self.spec["stages"][1]["outputs"]
        self.assertEqual(self.plan({"k": [1]}).returncode, 2)

    def test_retry_after_fix(self):
        self.run_modes("false_done")
        cid = next(iter(self.state()["cases"]))
        (self.base / "cases" / cid / "solve.py").write_text(
            'import json\njson.dump({"value": 9}, open("result.json", "w"))\n', encoding="utf-8")
        self.assertEqual(self.call(SWEEP, "retry", self.file, cid).returncode, 0)
        self.assertEqual(self.call(SWEEP, "advance", self.file, "--wait", 60).returncode, 0)


class Monitor(Queue):
    def submit(self, code, name="m"):
        script = self.dir / f"{name}.py"
        script.write_text(code, encoding="utf-8")
        r = self.call(SIMQ, "submit", "--line", "t", "--sw", "other", "--cores", "1", "--mem", "1", "--cwd", self.dir,
                      "--script", script, "--smoke", "--max-h", "0.1")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_marker_with_exit_zero_is_failed(self):
        job = self.submit('print("/*****Error")\n')
        self.assertEqual(self.call(SIMQ, "wait", job, "--timeout", 60).returncode, 1)
        self.assertIn("错误标记", self.call(SIMQ, "status", job).stdout)

    def test_metrics_measure_child_process_tree(self):
        job = self.submit('import subprocess,sys\n'
                          'subprocess.call([sys.executable,"-c","import time\\nt=time.time()\\n'
                          'x=bytearray(200*1024*1024)\\nwhile time.time()-t<22: pass"])\n')
        for _ in range(60):
            f = self.home / "jobs" / job / "metrics.json"
            if f.is_file() and json.loads(f.read_text(encoding="utf-8"))["cpu_s"] > 2:
                break
            time.sleep(1)
        r = self.call(SIMQ, "health")
        self.assertIn("PROGRESSING", r.stdout)
        self.call(SIMQ, "wait", job, "--timeout", 60)
        m = json.loads((self.home / "jobs" / job / "metrics.json").read_text(encoding="utf-8"))
        self.assertGreater(m["cpu_s"], 2)
        self.assertGreater(m["peak_rss_gb"], 0.15)


class Pure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("simq_under_test", SIMQ)
        cls.simq = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.simq)
        sys.path.insert(0, str(SCRIPTS))
        import sg
        cls.sg = sg

    def test_slow_is_not_stalled(self):
        v = self.simq.health_verdict
        self.assertEqual(v({"cpu_changed_ts": 1000, "log_changed_ts": 0}, 20, 1000 + 60), "PROGRESSING")
        self.assertEqual(v({"cpu_changed_ts": 0, "log_changed_ts": 1000}, 20, 1000 + 60), "PROGRESSING")
        self.assertEqual(v({"cpu_changed_ts": 0, "log_changed_ts": 0}, 20, 21 * 60), "STALLED")
        self.assertEqual(v(None, 20, 0), "NO_DATA")

    def test_mem_guard_only_hits_the_offender(self):
        t = self.simq.mem_guard_trips
        self.assertTrue(t(40, 30, 1, 4))
        self.assertFalse(t(29, 30, 1, 4))     # 没超声明，不牵连
        self.assertFalse(t(40, 30, 20, 4))    # 系统内存还够

    def test_classification(self):
        c = self.sg.classify_failure
        self.assertEqual(c("OutOfMemoryError: Java heap")[0], "memory")
        self.assertEqual(c("Could not obtain license for FDTD")[0], "license")
        self.assertEqual(c("Failed to find a solution")[0], "convergence")
        self.assertEqual(c("Traceback (most recent call last)")[0], "config")
        self.assertEqual(c("COMSOL license server: myserver")[0], "unknown")


if __name__ == "__main__":
    unittest.main()
