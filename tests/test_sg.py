# -*- coding: utf-8 -*-
"""运行：python -m unittest discover -s tests"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SG = ROOT / "skills" / "sim-guard" / "scripts" / "sg.py"
SIMQ = ROOT / "simq" / "simq.py"
GOOD_C = "2|2,'k','0[W/m/K]','0[W/m/K]','k'"
BAD_C = "3|3,'k','0','0','0','1','0','0','0','1'"


def run(*args, env=None):
    return subprocess.run([sys.executable, str(SG), *map(str, args)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def make_mph(path, c_value):
    xml = f"<Model><Physics tag='tte' op='CoefficientFormPDE'><PhysicsFeature tag='cfeq1'>" \
          f"<param param='c' value=\"{c_value}\"/></PhysicsFeature></Physics></Model>"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("dmodel.xml", xml)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.run_dir = self.dir / "run1"
        self.assertEqual(run("init", self.run_dir).returncode, 0)
        self.gate = {"question": "q", "criteria": [{"id": "G1", "metric": "m", "op": ">=", "threshold": 1}],
                     "validation": {"kind": "analytic_limit", "description": "1D", "shares_code_with_model": False},
                     "on_fail": "report"}
        self.manifest = {"run_id": "r", "line": "demo", "solver": "comsol", "derived_from": [], "drivers": [],
                         "outputs": [], "readback": {"comsol_tensor_models": ["m.mph"]}, "switch_tests": [],
                         "literature": [], "resources": {"cores": 1, "mem_gb": 1, "basis": "measured"},
                         "resume": {"supported": False, "why_not": "short"}, "jobs": []}
        self.script = self.run_dir / "run.ps1"
        self.script.write_text("exit 0\n", encoding="utf-8")
        make_mph(self.run_dir / "m.mph", GOOD_C)

    def write(self):
        (self.run_dir / "gate.json").write_text(json.dumps(self.gate), encoding="utf-8")
        (self.run_dir / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def ready(self):
        self.write()
        self.assertEqual(run("freeze", self.run_dir).returncode, 0)

    def preflight(self):
        return run("preflight", self.run_dir, "--script", self.script)


class Gate(Base):
    def test_template_cannot_be_frozen(self):
        self.assertEqual(run("freeze", self.run_dir).returncode, 1)

    def test_regression_is_not_validation(self):
        self.gate["validation"]["kind"] = "regression"
        self.write()
        self.assertEqual(run("freeze", self.run_dir).returncode, 1)

    def test_validation_sharing_code_rejected(self):
        self.gate["validation"]["shares_code_with_model"] = True
        self.write()
        self.assertEqual(run("freeze", self.run_dir).returncode, 1)

    def test_change_after_freeze_blocks_preflight(self):
        self.ready()
        self.gate["criteria"][0]["threshold"] = 0.1
        self.write()
        self.assertEqual(self.preflight().returncode, 1)
        self.assertEqual(run("freeze", self.run_dir).returncode, 1)

    def test_amend_keeps_history(self):
        self.ready()
        self.gate["criteria"][0]["threshold"] = 2
        self.write()
        self.assertEqual(run("freeze", self.run_dir, "--amend", "user approved").returncode, 0)
        lock = json.loads((self.run_dir / "gate.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["history"][0]["reason"], "user approved")


class Readback(Base):
    def test_partial_tensor_fails(self):
        make_mph(self.run_dir / "bad.mph", BAD_C)
        self.assertEqual(run("readback", self.run_dir / "bad.mph").returncode, 1)

    def test_isotropic_passes(self):
        self.assertEqual(run("readback", self.run_dir / "m.mph").returncode, 0)

    def test_preflight_blocks_bad_model(self):
        make_mph(self.run_dir / "m.mph", BAD_C)
        self.ready()
        r = self.preflight()
        self.assertEqual(r.returncode, 1)
        self.assertIn("张量读回不通过", r.stdout)
        self.assertFalse((self.run_dir / ".simguard" / "receipt.json").exists())

    def test_no_target_tensor_is_not_a_pass(self):
        with zipfile.ZipFile(self.run_dir / "empty.mph", "w") as z:
            z.writestr("dmodel.xml", "<Model/>")
        self.assertEqual(run("readback", self.run_dir / "empty.mph").returncode, 1)

    def test_declared_anisotropy_allowed(self):
        make_mph(self.run_dir / "m.mph", BAD_C)
        self.manifest["readback"]["anisotropic_ok"] = [{"physics": "tte", "feature": "cfeq1", "reason": "real"}]
        self.ready()
        self.assertEqual(self.preflight().returncode, 0)

    def test_comsol_param_readback(self):
        xml = ("<Model><expressions T='31' name='k' expr='2'/><Physics tag='tte' op='CoefficientFormPDE'>"
               f"<PhysicsFeature tag='cfeq1'><param param='c' value=\"{GOOD_C}\"/></PhysicsFeature></Physics></Model>")
        with zipfile.ZipFile(self.run_dir / "m.mph", "w") as z:
            z.writestr("dmodel.xml", xml)
        self.manifest["readback"]["comsol_params"] = {"k": 2.0}
        self.ready()
        self.assertEqual(self.preflight().returncode, 0)
        self.manifest["readback"]["comsol_params"] = {"k": 3}
        self.write()
        r = self.preflight()
        self.assertEqual(r.returncode, 1)
        self.assertIn("参数 k", r.stdout)

    def test_dump_checks(self):
        (self.run_dir / "rb.json").write_text(json.dumps({"s": {"w": 1.55e-6}, "n": [1, 2]}), encoding="utf-8")
        self.manifest["readback"] = {"dump": "rb.json", "checks": [
            {"path": "s.w", "op": "approx", "value": 1.55e-6, "rel": 1e-6}, {"path": "n", "op": "isotropic"}]}
        self.ready()
        r = self.preflight()
        self.assertEqual(r.returncode, 1)
        self.assertIn("n：读回", r.stdout)


class Usability(Base):
    def test_json_with_bom(self):
        self.write()
        f = self.run_dir / "gate.json"
        f.write_bytes(b"\xef\xbb\xbf" + f.read_bytes())
        self.assertEqual(run("freeze", self.run_dir).returncode, 0)

    def test_relative_script_resolves_against_run_dir(self):
        self.ready()
        self.assertEqual(run("preflight", self.run_dir, "--script", "run.ps1").returncode, 0)

    def test_model_solver_cannot_skip_readback(self):
        self.manifest["readback"] = {"not_applicable": "x"}
        self.ready()
        self.assertIn("不能跳过读回", self.preflight().stdout)


class Drivers(Base):
    def test_set_index_detected_and_allow_comment(self):
        f = self.dir / "D.java"
        f.write_text('feature("cfeq1").setIndex("c", k, 0);\n', encoding="utf-8")
        self.assertEqual(run("scan", f).returncode, 1)
        f.write_text('feature("cfeq1").setIndex("c", k, 0); // simguard: allow comsol-partial-tensor scalar\n',
                     encoding="utf-8")
        self.assertEqual(run("scan", f).returncode, 0)

    def test_marked_source_rejected(self):
        old = self.dir / "old"
        old.mkdir()
        (old / "D.java").write_text("// x\n", encoding="utf-8")
        (old / "DO_NOT_REUSE.md").write_text("bad\n", encoding="utf-8")
        self.manifest["derived_from"] = [str(old / "D.java")]
        self.ready()
        self.assertIn("不可复用", self.preflight().stdout)

    def test_lumapi_needs_lumerical(self):
        self.script = self.run_dir / "run.py"
        self.script.write_text("import lumapi\n", encoding="utf-8")
        self.manifest["solver"] = "python"
        self.manifest["readback"] = {"not_applicable": "x"}
        self.ready()
        self.assertIn("lumapi", self.preflight().stdout)

    def test_readonly_scope(self):
        (self.dir / "simguard.project.json").write_text(json.dumps({"readonly_paths": ["run1"]}), encoding="utf-8")
        self.ready()
        self.assertIn("只读范围", self.preflight().stdout)

    def test_failed_switch_test_blocks(self):
        self.manifest["switch_tests"] = [{"switch": "coulomb=False", "test": "T", "status": "NOT_DONE"}]
        self.ready()
        self.assertIn("开关测试未通过", self.preflight().stdout)


class Report(Base):
    def report(self, overall, conv="PASS"):
        (self.run_dir / "REPORT.md").write_text(
            f"Overall: {overall}\nG1 ok\n| check | s | e |\n|---|---|---|\n"
            f"| resolution_convergence | {conv} | {'x' if conv == 'PASS' else ''} |\n"
            "| independent_validation | PASS | x |\n| readback | PASS | x |\n", encoding="utf-8")

    def test_template_rejected(self):
        self.ready()
        self.assertEqual(run("report", self.run_dir).returncode, 1)

    def test_pass_with_missing_check_rejected(self):
        self.ready()
        self.report("PASS", "NOT_DONE")
        self.assertEqual(run("report", self.run_dir).returncode, 1)

    def test_partial_with_missing_check_ok(self):
        self.ready()
        self.report("PARTIAL", "NOT_DONE")
        self.assertEqual(run("report", self.run_dir).returncode, 0)
        self.assertTrue((self.run_dir / "provenance.json").exists())


class SimqGate(Base):
    """用临时 SIMQ_HOME，不碰在用的队列；任务提交后立刻取消。"""

    def setUp(self):
        super().setUp()
        self.home = self.dir / "simq_home"
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({
            "cores": 2, "mem_gb": 2, "min_free_mem_gb": 0, "licenses": {}, "require_preflight": True,
            "allow_prio_override": False}), encoding="utf-8")
        self.env = dict(os.environ, SIMQ_HOME=str(self.home))
        self.addCleanup(self.stop)

    def stop(self):
        subprocess.run([sys.executable, str(SIMQ), "dispatcher", "stop"], env=self.env, capture_output=True)
        import time
        for _ in range(40):
            if not (self.home / "DISPATCHER_STOP").exists():
                break
            time.sleep(0.5)
        time.sleep(1)

    def simq(self, *args):
        return subprocess.run([sys.executable, str(SIMQ), "submit", "--line", "demo", "--sw", "other", "--cores", "1",
                               "--mem", "1", "--cwd", str(self.run_dir), *args], env=self.env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    def test_no_receipt_refused(self):
        r = self.simq("--script", str(self.script))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("preflight", r.stderr)

    def test_prio_refused(self):
        self.assertIn("--prio", self.simq("--script", str(self.script), "--prio", "99").stderr)

    def test_smoke_needs_max_h(self):
        self.assertIn("--max-h", self.simq("--script", str(self.script), "--smoke").stderr)

    def test_receipt_flow_and_stale_script(self):
        self.ready()
        self.assertEqual(self.preflight().returncode, 0)
        self.script.write_text("exit 1\n", encoding="utf-8")
        self.assertIn("改过", self.simq("--script", str(self.script)).stderr)
        self.assertEqual(self.preflight().returncode, 0)
        r = self.simq("--script", str(self.script))
        self.assertEqual(r.returncode, 0, r.stderr)
        job = json.loads((self.home / "jobs" / r.stdout.strip() / "job.json").read_text(encoding="utf-8"))
        self.assertIn("script_sha256", job["preflight"])


class Recovery(unittest.TestCase):
    """不启动调度器，也不调用求解器，直接验证丢失后的处理。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        code = (
            "import json,os,pathlib,sys,time\n"
            f"sys.path.insert(0,{str(SIMQ.parent)!r})\n"
            "import simq\n"
            "d=simq.job_dir('j');d.mkdir(parents=True)\n"
            "started=simq.now()-simq.datetime.timedelta(hours=1)\n"
            "j=dict(id='j',line='x',name='n',sw='other',cores=1,mem=1,status='running',prio=0,\n"
            "       submitted=started.isoformat(),started=started.isoformat(),argv=['a'],**json.loads(sys.argv[1]))\n"
            "simq.write_json(d/'job.json',j)\n"
            "(d/'heartbeat').write_text('0');os.utime(d/'heartbeat',(1,1))\n"
            "simq.write_json(d/'runner.json',{'runner_pid':999999,'child_pid':999998,'started':started.isoformat()})\n"
            "j=simq.load_job('j');simq.update_running(simq.load_config(),j)\n"
            "print(json.dumps(simq.read_json(d/'job.json')))\n"
        )
        self.code = code

    def lost(self, extra):
        env = dict(os.environ, SIMQ_HOME=str(self.home))
        r = subprocess.run([sys.executable, "-c", self.code, json.dumps(extra)], env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_requeued_with_resume_script(self):
        j = self.lost({"resume_argv": ["resume"], "max_resume": 2})
        self.assertEqual((j["status"], j["argv"], j["resume_count"]), ("pending", ["resume"], 1))
        self.assertNotIn("started", j)
        self.assertFalse((self.home / "jobs" / "j" / "heartbeat").exists())

    def test_retry_limit(self):
        j = self.lost({"resume_argv": ["resume"], "max_resume": 2, "resume_count": 2})
        self.assertEqual(j["status"], "lost")

    def test_no_resume_script_stays_lost(self):
        self.assertEqual(self.lost({})["status"], "lost")


class Autostart(unittest.TestCase):
    def test_print_does_not_install(self):
        r = subprocess.run([sys.executable, str(SIMQ), "autostart", "print"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=dict(os.environ, SIMQ_HOME=tempfile.gettempdir()))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("/SC ONLOGON", r.stdout)
        self.assertIn("_dispatch", r.stdout)


if __name__ == "__main__":
    unittest.main()
