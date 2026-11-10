# -*- coding: utf-8 -*-
"""运行：python -m unittest discover -s tests"""
import os

os.environ["SIMGUARD_LANG"] = "zh"   # 先于导入 sg 设置；子进程继承，现有中文断言保持成立

import json
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
        self.assertIn("physics/tte/cfeq1/c", r.stdout)
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
        self.assertIn("param/k", r.stdout)

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
    def report(self, overall, conv="PASS", g1="PASS", rb="PASS"):
        (self.run_dir / "REPORT.md").write_text(
            f"Overall: {overall}\n| id | v | t | c |\n|---|---|---|---|\n| G1 | 2 | >=1 | {g1} |\n"
            f"| check | s | e |\n|---|---|---|\n"
            f"| resolution_convergence | {conv} | {'x' if conv == 'PASS' else ''} |\n"
            f"| independent_validation | PASS | x |\n| readback | {rb} | x |\n", encoding="utf-8")

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

    def test_failed_criterion_blocks_pass(self):
        self.ready()
        self.report("PASS", g1="FAIL")
        r = run("report", self.run_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("判据 ['G1']", r.stdout)

    def test_criterion_id_must_be_its_own_row(self):
        self.gate["criteria"][0]["id"] = "1"          # 旧版只做子串匹配，"1" 几乎总能匹配上
        self.ready()
        (self.run_dir / "REPORT.md").write_text(
            "Overall: PARTIAL\n| check | s | e |\n|---|---|---|\n| resolution_convergence | NOT_DONE | |\n"
            "| independent_validation | PASS | x1 |\n| readback | PASS | x1 |\n", encoding="utf-8")
        self.assertIn("判据 1 要正好一行", run("report", self.run_dir).stdout)

    def test_na_not_allowed_for_readback(self):
        self.ready()
        self.report("PARTIAL", rb="NA")
        self.assertIn("readback 不能写 NA", run("report", self.run_dir).stdout)

    def test_nan_result_blocks_pass(self):
        self.manifest["outputs"] = ["result.json"]
        self.ready()
        (self.run_dir / "result.json").write_text('{"T": NaN}', encoding="utf-8")
        self.report("PASS")
        r = run("report", self.run_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("NaN", r.stdout)

    def test_result_expectations(self):
        self.manifest["outputs"] = ["result.json"]
        self.manifest["result_expect"] = [
            {"op": "sum_approx", "keys": ["result:R", "result:T", "result:A"], "value": 1, "rel": 1e-3},
            {"op": "converged", "keys": ["result:coarse", "result:fine"], "rel": 0.01}]
        self.ready()
        self.report("PASS")
        good = {"R": 0.1, "T": 0.85, "A": 0.05, "coarse": 1.0, "fine": 1.001}
        (self.run_dir / "result.json").write_text(json.dumps(good), encoding="utf-8")
        self.assertEqual(run("report", self.run_dir).returncode, 0)
        (self.run_dir / "result.json").write_text(json.dumps(dict(good, A=0.2)), encoding="utf-8")
        self.assertIn("守恒没闭合", run("report", self.run_dir).stdout)
        (self.run_dir / "result.json").write_text(json.dumps(dict(good, fine=1.0)), encoding="utf-8")
        self.assertIn("不可能失败", run("report", self.run_dir).stdout)

    def test_refreeze_after_submit_refused(self):
        self.ready()
        m = json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))
        m["jobs"] = ["1108-120000-demo-abc"]
        m["job_gates"] = {"1108-120000-demo-abc": "0" * 64}
        (self.run_dir / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        (self.run_dir / "gate.lock.json").unlink()
        self.assertIn("冻结记录不见了", run("freeze", self.run_dir).stdout)
        # 就算绕过 freeze 重建了锁，报告也会发现提交时的判据对不上
        (self.run_dir / "gate.lock.json").write_text(json.dumps(
            {"sha256": __import__("hashlib").sha256((self.run_dir / "gate.json").read_bytes()).hexdigest(),
             "history": []}), encoding="utf-8")
        self.report("PASS")
        self.assertIn("冻结记录可能被删掉重建过", run("report", self.run_dir).stdout)


class General(Base):
    """通用检查：不依赖物理场名字，也能抓住同一类错误。"""

    def mph(self, xml, name="g.mph"):
        with zipfile.ZipFile(self.run_dir / name, "w") as z:
            z.writestr("dmodel.xml", f"<Model>{xml}</Model>")
        return self.run_dir / name

    def test_partial_tensor_caught_without_tags(self):
        f = self.mph("<Physics tag='ht' op='HeatTransfer'><PhysicsFeature tag='solid1'>"
                     "<param param='k' value=\"1|9,'kx','0','0','0','1','0','0','0','1'\"/></PhysicsFeature></Physics>")
        r = run("readback", f)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("physics/ht/solid1/k", r.stdout)

    def test_material_tensor_format(self):
        bad = "9|1,'sig'|1,'0'|1,'0'|1,'0'|1,'1'|1,'0'|1,'0'|1,'0'|1,'1'"
        good = "9|1,'sig'|1,'0'|1,'0'|1,'0'|1,'sig'|1,'0'|1,'0'|1,'0'|1,'sig'"
        mat = "<Material tag='mat1'><MaterialModel tag='def'><param param='electricconductivity' value=\"{}\"/>" \
              "</MaterialModel></Material>"
        self.assertEqual(run("readback", self.mph(mat.format(bad))).returncode, 1)
        self.assertEqual(run("readback", self.mph(mat.format(good))).returncode, 0)

    def test_sources_and_expect(self):
        self.mph("<expressions name='lambda0' expr='1550[nm]'/>", "model.mph")
        (self.run_dir / "rb.json").write_text(json.dumps({"mesh": {"dx": 2e-8}}), encoding="utf-8")
        self.manifest["readback"] = {"sources": {"model": "model.mph", "rb": "rb.json"}, "expect": [
            {"select": "model:param/lambda0", "op": "approx", "value": 1550, "rel": 1e-9},
            {"select": "rb:mesh.dx", "op": "range", "max": 5e-8}]}
        self.ready()
        self.assertEqual(self.preflight().returncode, 0)
        self.manifest["readback"]["expect"][0]["value"] = 1310
        self.write()
        self.assertEqual(run("freeze", self.run_dir, "--amend", "test").returncode, 0)
        self.assertIn("model:param/lambda0", self.preflight().stdout)

    def test_select_matching_nothing_fails(self):
        self.manifest["readback"] = {"sources": {"m": "m.mph"},
                                     "expect": [{"select": "m:param/typo", "op": "present"}]}
        self.ready()
        self.assertIn("没有匹配到任何值", self.preflight().stdout)

    def test_response_test_is_computed(self):
        (self.run_dir / "on.json").write_text('{"heat": 5.0}', encoding="utf-8")
        (self.run_dir / "off.json").write_text('{"heat": 5.0}', encoding="utf-8")
        self.manifest["response_tests"] = [{"id": "R1", "knob": "joule=off", "expect": "decreases",
                                            "baseline": {"file": "on.json", "key": "heat"},
                                            "perturbed": {"file": "off.json", "key": "heat"}, "status": "PASS"}]
        self.ready()
        r = self.preflight()
        self.assertEqual(r.returncode, 1)            # 自己写的 status 不算数，按数字判
        self.assertIn("R1", r.stdout)
        (self.run_dir / "off.json").write_text('{"heat": 0.01}', encoding="utf-8")
        self.assertEqual(self.preflight().returncode, 0)

    def test_run_dir_driver_scanned_without_listing(self):
        (self.run_dir / "Driver.java").write_text('m.feature("f").setIndex(\n    "c", "k", 0);\n', encoding="utf-8")
        self.ready()
        self.assertIn("comsol-partial-tensor", self.preflight().stdout)

    def test_python_set_index_single_quotes(self):
        f = self.dir / "drv.py"
        f.write_text("feat.setIndex('c', 'k', 0)\n", encoding="utf-8")
        self.assertEqual(run("scan", f).returncode, 1)

    def test_marker_in_parent_dir(self):
        old = self.dir / "old"
        (old / "drivers").mkdir(parents=True)
        (old / "drivers" / "D.java").write_text("// x\n", encoding="utf-8")
        (old / "DO_NOT_REUSE.md").write_text("bad\n", encoding="utf-8")
        self.manifest["derived_from"] = [str(old / "drivers" / "D.java")]
        self.ready()
        self.assertIn("不可复用", self.preflight().stdout)

    def test_taint_finds_descendants(self):
        self.ready()
        bad = self.dir / "bad_run"
        bad.mkdir()
        child = self.dir / "child"
        grandchild = self.dir / "grandchild"
        for d, src in ((child, bad), (grandchild, child)):
            d.mkdir()
            (d / "manifest.json").write_text(json.dumps({"derived_from": [str(src)]}), encoding="utf-8")
        r = run("taint", bad, "--reason", "tensor bug", "--root", self.dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("child", r.stdout)
        self.assertIn("grandchild", r.stdout)
        self.assertTrue((bad / "DO_NOT_REUSE.md").exists())

    def test_job_id_parsing_ignores_warnings(self):
        sys.path.insert(0, str(SG.parent))
        import sg
        self.assertEqual(sg.parse_job_id("1108-120000-demo-abc\n警告：调度器没能启动\n"), "1108-120000-demo-abc")
        self.assertIsNone(sg.parse_job_id("警告：调度器没能启动\n"))


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

    def test_receipt_covers_drivers_and_model(self):
        drv = self.run_dir / "Driver.java"
        drv.write_text("// ok\n", encoding="utf-8")
        self.ready()
        self.assertEqual(self.preflight().returncode, 0)
        drv.write_text("// changed after preflight\n", encoding="utf-8")
        self.assertIn("Driver.java", self.simq("--script", str(self.script)).stderr)
        self.assertEqual(self.preflight().returncode, 0)
        make_mph(self.run_dir / "m.mph", BAD_C.replace("'1'", "'k'"))    # 读回过的模型被换掉
        self.assertIn("m.mph", self.simq("--script", str(self.script)).stderr)

    def test_relative_script_resolves_against_cwd(self):
        self.ready()
        self.assertEqual(self.preflight().returncode, 0)
        r = self.simq("--script", "run.ps1")       # 进程当前目录不是 run 目录
        self.assertEqual(r.returncode, 0, r.stderr)


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

    def test_alive_runner_is_not_lost(self):
        """心跳停了但运行器还活着（例如电脑睡眠后醒来）：不能判为丢失。"""
        code = self.code.replace(
            "simq.write_json(d/'runner.json',{'runner_pid':999999,'child_pid':999998,'started':started.isoformat()})",
            "simq.write_json(d/'runner.json',{'runner_pid':os.getpid(),'child_pid':os.getpid(),"
            "'started':simq.now_iso()})")
        self.assertNotEqual(code, self.code)
        self.code = code
        self.assertEqual(self.lost({})["status"], "running")


class Autostart(unittest.TestCase):
    def test_print_does_not_install(self):
        r = subprocess.run([sys.executable, str(SIMQ), "autostart", "print"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=dict(os.environ, SIMQ_HOME=tempfile.gettempdir()))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("/SC ONLOGON", r.stdout)
        self.assertIn("_dispatch", r.stdout)


class Language(unittest.TestCase):
    """SIMGUARD_LANG 切换输出语言；失败分类同时认中英文日志。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def sg(self, lang, *args):
        return run(*args, env=dict(os.environ, SIMGUARD_LANG=lang))

    def test_english_error_and_template(self):
        r = self.sg("en", "readback", self.dir / "missing.mph")
        self.assertEqual(r.returncode, 2)
        self.assertIn("error: ", r.stderr)
        self.assertIn("cannot read model settings", r.stderr)
        self.assertNotIn("读不出", r.stderr)
        run_dir = self.dir / "run_en"
        r = self.sg("en", "init", run_dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("created: REPORT.md", r.stdout)
        report = (run_dir / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("## Criteria results", report)
        self.assertIn("TODO the one question", (run_dir / "gate.json").read_text(encoding="utf-8"))

    def test_both_languages(self):
        r = self.sg("both", "readback", self.dir / "missing.mph")
        self.assertIn("读不出模型设置", r.stderr)
        self.assertIn("cannot read model settings", r.stderr)
        run_dir = self.dir / "run_both"
        r = self.sg("both", "init", run_dir)
        self.assertIn("已生成：REPORT.md / created: REPORT.md", r.stdout)
        self.assertIn("## 判据结果", (run_dir / "REPORT.md").read_text(encoding="utf-8"))

    def test_classify_failure_english(self):
        sys.path.insert(0, str(SG.parent))
        import sg
        self.assertEqual(sg.classify_failure("exceeded declared memory")[0], "memory")
        self.assertEqual(sg.classify_failure("COMSOL license pool full (2)")[0], "license")
        self.assertEqual(sg.classify_failure("超过声明内存")[0], "memory")
        self.assertEqual(sg.classify_failure("compile failed")[0], "config")
        self.assertEqual(sg.classify_failure("script does not exist")[0], "config")


if __name__ == "__main__":
    unittest.main()
