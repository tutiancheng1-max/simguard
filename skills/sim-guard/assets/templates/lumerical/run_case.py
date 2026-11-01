# -*- coding: utf-8 -*-
"""
FDTD 模板。build 阶段建模、保存、再重新打开并导出读回；solve 阶段读取保存的工程求解。
先把这个算例跑通再改物理。

算例：平面波正入射 n=1 到 n=1.5 的界面。解析解：透射率 T = 4 n / (1 + n)^2 = 0.96。
"""
import glob
import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE / "prepared.fsp"
PARAMS_FILE = HERE / "params.json"    # 参数扫描的取值
PARAMS = json.loads(PARAMS_FILE.read_text(encoding="utf-8-sig")) if PARAMS_FILE.exists() else {}
INDEX = float(PARAMS.get("index", 1.5))
WAVELENGTH_MIN = 1.5e-6
WAVELENGTH_MAX = 1.6e-6


def lumerical_root():
    configured = os.environ.get("LUMERICAL_ROOT")
    if configured:
        return pathlib.Path(configured)
    found = sorted(glob.glob(r"C:\Program Files\ANSYS Inc\v*\Lumerical"))
    if not found:
        sys.exit("找不到 Lumerical：请设置环境变量 LUMERICAL_ROOT")
    return pathlib.Path(found[-1])


def open_fdtd():
    """lumapi 需要 numpy；当前 Python 没有时，切换到 Lumerical 自带的 Python 重新运行。"""
    root = lumerical_root()
    try:
        import numpy  # noqa: F401
    except ImportError:
        bundled = sorted(root.glob("python-*/python.exe"))
        if not bundled or pathlib.Path(sys.executable) == bundled[-1]:
            sys.exit("找不到带 numpy 的 Python")
        sys.exit(subprocess.call([str(bundled[-1]), "-u", sys.argv[0]]))
    sys.path.insert(0, str(root / "api" / "python"))
    import lumapi
    return lumapi.FDTD(hide=True)


def build(s):
    s.addfdtd(dimension="2D", x=0, x_span=0.5e-6, y_min=-3e-6, y_max=4e-6, mesh_accuracy=2,
              simulation_time=200e-15, x_min_bc="Periodic")
    s.addrect(name="slab", x=0, x_span=2e-6, y_min=-6e-6, y_max=0)
    s.setnamed("slab", "material", "<Object defined dielectric>")
    s.setnamed("slab", "index", INDEX)
    s.addplane(name="src", injection_axis="y", direction="Backward", x=0, x_span=1e-6, y=2e-6,
               wavelength_start=WAVELENGTH_MIN, wavelength_stop=WAVELENGTH_MAX)
    s.addpower(name="T", monitor_type="Linear X", x=0, x_span=1e-6, y=-1e-6)
    s.save(str(PROJECT))


def dump_readback(s):
    s.load(str(PROJECT))
    data = {"slab": {"index": float(s.getnamed("slab", "index"))},
            "source": {"wavelength_start_m": float(s.getnamed("src", "wavelength start")),
                       "wavelength_stop_m": float(s.getnamed("src", "wavelength stop"))},
            "fdtd": {"x_min_bc": str(s.getnamed("FDTD", "x min bc"))}}
    (HERE / "readback.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def solve(s):
    result = {}
    for label, accuracy in (("coarse", 2), ("fine", 4)):
        s.load(str(PROJECT))
        s.setnamed("FDTD", "mesh accuracy", accuracy)
        s.save(str(HERE / f"solve_{label}.fsp"))    # 另存，不覆盖读回过的工程
        s.setresource("FDTD", 1, "processes", int(os.environ.get("SIMQ_CORES", "2")))
        s.run()
        t = s.transmission("T")
        result[f"T_{label}"] = float(abs(sum(float(x) for x in t.ravel()) / t.size))
    (HERE / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


def main(stage):
    s = open_fdtd()
    try:
        if stage == "build":
            build(s)
            dump_readback(s)
        else:
            solve(s)
    finally:
        s.close()
