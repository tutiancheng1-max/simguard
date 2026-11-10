# -*- coding: utf-8 -*-
"""
simq —— 单机共享的仿真队列（Windows）

所有对话里的子 agent 都通过它跑长仿真，不直接起求解器。
求解进程由后台调度器按核数、内存、license 统一发车，与提交它的子 agent 的寿命无关。

常用：
  python simq.py submit --line demo --name "case 3D" --sw comsol --cores 16 --mem 30 --est 3 \
                        --cwd D:\\run_dir --cmd "comsolbatch -inputfile model.mph -outputfile out.mph"
  python simq.py submit ... --script run_case.ps1        # 用脚本代替 --cmd（推荐，避免引号嵌套）
  python simq.py list [--all] [--line demo]
  python simq.py status <id>          python simq.py tail <id> [-n 40]
  python simq.py wait <id> [--timeout 540]              # 最多阻塞 timeout 秒
  python simq.py cancel <id>
  python simq.py res                  # 资源占用
  python simq.py dispatcher [start|stop|status]
  python simq.py idle                 # 队列空且无任务在跑 → 退出码 0

<id> 可以只写能唯一识别的一段。
"""
import argparse
import ctypes
import datetime
import hashlib
import json
import os
import pathlib
import random
import re
import string
import subprocess
import sys
import threading
import time


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


HERE =pathlib.Path(__file__).resolve().parent
DATA = pathlib.Path(os.environ.get("SIMQ_HOME") or HERE)   # 配置、任务和日志放在这里
JOBS = DATA / "jobs"
CONFIG = DATA / "config.json"
DISP_FILE = DATA / "dispatcher.json"
DISP_LOCK = DATA / "dispatcher.lock"
DISP_STOP = DATA / "DISPATCHER_STOP"
DISP_LOG = DATA / "dispatcher.log"

DEFAULT_CONFIG = {
    "cores": max(1, (os.cpu_count() or 2) - 2),   # 可分配的总线程数（留 2 个给系统和交互）
    "mem_gb": 8,                 # 可分配的总内存；保守默认值，请按本机改 config.json
    "min_free_mem_gb": 4,        # 发车后实际空闲内存至少剩这么多
    "licenses": {"comsol": 1, "lumerical": 1, "fluent": 1, "matlab": 1},
    "line_priority": {},         # 例如 {"paper-a": 10}；数字大的先跑
    "poll_s": 10,
    "reserve_after_h": 2,        # 排在最前的任务等了这么久还没发车，就不再让后面的小任务插队
    "shutdown_vote": None,       # 可选：外部关机守卫脚本，参数为 hold|done <名字> <备注>
    "require_preflight": False,  # true：正式任务必须带 sim-guard 的 preflight 回执
    "smoke_max_h": 0.5,          # --smoke 任务允许的 --max-h 上限
    "allow_prio_override": True, # false：不允许用 --prio 覆盖研究线优先级
    "stall_min": 20,             # CPU 和日志都这么久没变化，health 判为 STALLED（只报告，不杀）
    "mem_guard": False,          # true：任务超过声明内存且系统内存将耗尽时，终止这个任务
    "error_markers": ["/*****Error"],   # 退出码为 0 但日志里有这些文字，判为 failed
}
SW_CHOICES = ["comsol", "lumerical", "fluent", "matlab", "python", "other"]
ACTIVE = ("pending", "running")
FINAL_OK = ("done",)

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------- 通用工具

def now():
    return datetime.datetime.now().replace(microsecond=0)


def now_iso():
    return now().isoformat()


def parse_iso(s):
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


def fmt_dur(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    return f"{h}h{rem // 60:02d}m" if h else f"{rem // 60}m{rem % 60:02d}s"


def read_json(path, default=None):
    for _ in range(5):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except (PermissionError, json.JSONDecodeError):
            time.sleep(0.2)
    return default


def write_json(path, data):
    path = pathlib.Path(path)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    user = read_json(CONFIG, None)
    if user is None:
        write_json(CONFIG, DEFAULT_CONFIG)
    else:
        cfg.update(user)
    return cfg


class _MEMSTAT(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def free_mem_gb():
    st = _MEMSTAT()
    st.dwLength = ctypes.sizeof(_MEMSTAT)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    return st.ullAvailPhys / 1024 ** 3


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong), ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong), ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_char * 260)]


class _MEMCOUNTERS(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


_K32 = ctypes.WinDLL("kernel32", use_last_error=True)
_K32.OpenProcess.restype = ctypes.c_void_p
_K32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
_K32.CloseHandle.argtypes = [ctypes.c_void_p]
_K32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_K32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
_K32.Process32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32)]
_K32.Process32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32)]
_K32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
_K32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MEMCOUNTERS), ctypes.c_ulong]
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def process_tree(root):
    """root 和它所有子孙进程的 PID。"""
    snap = _K32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
    if not snap or snap == _INVALID_HANDLE:
        return [root]
    children = {}
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _K32.Process32First(snap, ctypes.byref(entry))
        while ok:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            ok = _K32.Process32Next(snap, ctypes.byref(entry))
    finally:
        _K32.CloseHandle(snap)
    tree, stack = [], [root]
    while stack:
        pid = stack.pop()
        if pid in tree:
            continue
        tree.append(pid)
        stack.extend(children.get(pid, []))
    return tree


def tree_usage(root):
    """返回 (CPU 秒数, 内存 GB)，统计整棵进程树。"""
    cpu, rss = 0.0, 0
    for pid in process_tree(root):
        handle = _K32.OpenProcess(0x1000, 0, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            t = [ctypes.c_ulonglong() for _ in range(4)]
            if _K32.GetProcessTimes(handle, *[ctypes.byref(x) for x in t]):
                cpu += (t[2].value + t[3].value) / 1e7
            mem = _MEMCOUNTERS()
            mem.cb = ctypes.sizeof(mem)
            if _K32.K32GetProcessMemoryInfo(handle, ctypes.byref(mem), mem.cb):
                rss += mem.WorkingSetSize
        finally:
            _K32.CloseHandle(handle)
    return cpu, rss / 1024 ** 3


def mem_guard_trips(rss_gb, declared_gb, free_gb, min_free_gb):
    """只有任务超过自己的声明、系统内存又快耗尽时才触发，不牵连守规矩的任务。"""
    return rss_gb > declared_gb * 1.1 and free_gb < min_free_gb / 2


def health_verdict(metrics, stall_min, now_ts):
    """CPU 或日志有变化就算在推进；慢不算问题。"""
    if not metrics:
        return "NO_DATA"
    latest = max(metrics.get("cpu_changed_ts", 0), metrics.get("log_changed_ts", 0))
    return "STALLED" if now_ts - latest > stall_min * 60 else "PROGRESSING"


def console_python():
    exe = pathlib.Path(sys.executable)
    cand = exe.with_name("python.exe")
    return str(cand if cand.exists() else exe)


def windowless_python():
    exe = pathlib.Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


def spawn_detached(argv):
    """起一个与当前进程（及其 Job 对象）脱钩的后台进程。"""
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True, cwd=str(HERE))
    try:
        return subprocess.Popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:
        return subprocess.Popen(argv, creationflags=flags, **kw)


# ---------------------------------------------------------------- 任务读写

def job_dir(jid):
    return JOBS / jid


def load_job(jid):
    j = read_json(job_dir(jid) / "job.json")
    if j is not None:
        j["_dir"] = str(job_dir(jid))
    return j


def save_job(j):
    data = {k: v for k, v in j.items() if not k.startswith("_")}
    write_json(pathlib.Path(j["_dir"]) / "job.json", data)


def all_jobs():
    out = []
    if JOBS.exists():
        for d in sorted(JOBS.iterdir()):
            if d.is_dir():
                j = load_job(d.name)
                if j:
                    out.append(j)
    return out


def resolve_id(fragment):
    ids = [d.name for d in JOBS.iterdir() if d.is_dir()] if JOBS.exists() else []
    if fragment in ids:
        return fragment
    hits = [i for i in ids if fragment in i]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        sys.exit(T(f"找不到任务：{fragment}", f"job not found: {fragment}"))
    sys.exit(T(f"'{fragment}' 匹配到多个任务：" + "、".join(hits),
               f"'{fragment}' matches several jobs: " + ", ".join(hits)))


def new_id(line):
    slug = re.sub(r"[^A-Za-z0-9]+", "", line)[:12] or "job"
    tail = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(3))
    return f"{now().strftime('%m%d-%H%M%S')}-{slug}-{tail}"


def job_cancel_reason(j):
    f = pathlib.Path(j["_dir"]) / "CANCEL"
    if not f.exists():
        return None
    try:
        return f.read_text(encoding="utf-8").strip() or "user"
    except Exception:
        return "user"


# ---------------------------------------------------------------- 关机协调（可选）

def vote(cfg, action, name, note=""):
    script = cfg.get("shutdown_vote")
    if not script or not pathlib.Path(script).exists():
        return
    try:
        subprocess.run([console_python(), script, action, name, note], cwd=str(pathlib.Path(script).parent),
                       capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        dlog(T(f"关机登记失败 {action} {name}: {e}", f"shutdown vote failed {action} {name}: {e}"))


# ---------------------------------------------------------------- 运行器：每个任务一个，负责起求解器、心跳、取消

def runner(jdir):
    jdir = pathlib.Path(jdir)
    j = read_json(jdir / "job.json")
    env = dict(os.environ, SIMQ_JOB_ID=j["id"], SIMQ_CORES=str(j["cores"]), SIMQ_JOB_DIR=str(jdir),
               SIMQ_RESUME_COUNT=str(j.get("resume_count", 0)))
    log = open(jdir / "output.log", "ab", buffering=0)
    log.write((T(f"=== simq {j['id']} 开始 {now_iso()} | cwd={j['cwd']} ===",
                 f"=== simq {j['id']} start {now_iso()} | cwd={j['cwd']} ===") + "\n").encode("utf-8"))
    kw = dict(cwd=j["cwd"], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
              env=env, creationflags=CREATE_NO_WINDOW)
    try:
        if j.get("argv"):
            p = subprocess.Popen(j["argv"], **kw)
        else:
            p = subprocess.Popen(j["cmd"], shell=True, **kw)
    except Exception as e:
        log.write((T(f"=== 启动失败：{e} ===", f"=== launch failed: {e} ===") + "\n").encode("utf-8"))
        write_json(jdir / "exit.json", {"code": -1, "ended": now_iso(), "error": str(e)})
        return
    write_json(jdir / "runner.json", {"runner_pid": os.getpid(), "child_pid": p.pid, "started": now_iso()})

    stop = threading.Event()

    metrics = {"cpu_s": 0.0, "rss_gb": 0.0, "peak_rss_gb": 0.0, "log_bytes": 0,
               "cpu_changed_ts": time.time(), "log_changed_ts": time.time()}

    def sample():
        try:
            cpu, rss = tree_usage(p.pid)
            size = (jdir / "output.log").stat().st_size
        except OSError:
            return
        t = time.time()
        if cpu > metrics["cpu_s"] + 1:     # 子进程退出时总量会下降，只看增长
            metrics.update(cpu_s=cpu, cpu_changed_ts=t)
        if size != metrics["log_bytes"]:
            metrics.update(log_bytes=size, log_changed_ts=t)
        metrics.update(rss_gb=rss, peak_rss_gb=max(metrics["peak_rss_gb"], rss), ts=t)
        write_json(jdir / "metrics.json", metrics)
        cfg = load_config()
        if cfg.get("mem_guard") and not (jdir / "CANCEL").exists() and \
                mem_guard_trips(rss, j["mem"], free_mem_gb(), cfg.get("min_free_mem_gb", 4)):
            (jdir / "CANCEL").write_text("memory", encoding="utf-8")

    def beat():
        killed = False
        while not stop.is_set():
            (jdir / "heartbeat").write_text(str(time.time()))
            sample()
            if not killed and (jdir / "CANCEL").exists():
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                               creationflags=CREATE_NO_WINDOW)
                killed = True
            stop.wait(10)

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    code = p.wait()
    stop.set()
    log.write(("\n" + T(f"=== simq {j['id']} 结束 {now_iso()} | 退出码 {code} ===",
                        f"=== simq {j['id']} end {now_iso()} | exit code {code} ===") + "\n").encode("utf-8"))
    write_json(jdir / "exit.json", {"code": code, "ended": now_iso()})


# ---------------------------------------------------------------- 调度器

def dlog(msg):
    try:
        with open(DISP_LOG, "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {msg}\n")
    except Exception:
        pass


def finalize(cfg, j, status, reason=None, code=None):
    j["status"] = status
    j["ended"] = now_iso()
    if code is not None:
        j["exit_code"] = code
    if reason:
        j["reason"] = reason
    j.pop("wait_reason", None)
    save_job(j)
    dlog(f"{j['id']} → {status}" + (T(f"（{reason}）", f" ({reason})") if reason else ""))
    if j.get("started"):
        vote(cfg, "done", f"simq {j['id']}", status)


def log_has_error(cfg, jdir):
    """有些求解器出错时退出码也是 0。"""
    try:
        data = (pathlib.Path(jdir) / "output.log").read_bytes()[-2_000_000:].decode("utf-8", errors="replace")
    except OSError:
        return False
    return any(m in data for m in cfg.get("error_markers", []))


def update_running(cfg, j):
    d = pathlib.Path(j["_dir"])
    ex = read_json(d / "exit.json")
    cancel = job_cancel_reason(j)
    if ex is not None:
        code = ex.get("code")
        if cancel:
            status = {"timeout": "timeout", "memory": "failed"}.get(cancel, "cancelled")
            finalize(cfg, j, status,
                     T("超过声明内存且系统内存将耗尽，已终止",
                       "exceeded declared memory and system memory nearly exhausted; killed")
                     if cancel == "memory" else cancel, code)
        elif code == 0 and log_has_error(cfg, d):
            finalize(cfg, j, "failed", T("退出码为 0，但日志里有错误标记", "exit code 0 but the log has error markers"), code)
        else:
            finalize(cfg, j, "done" if code == 0 else "failed",
                     ex.get("error") or (None if code == 0 else T(f"退出码 {code}", f"exit code {code}")), code)
        return
    started = parse_iso(j.get("started", ""))
    hb = d / "heartbeat"
    age = time.time() - hb.stat().st_mtime if hb.exists() else None
    since_start = (now() - started).total_seconds() if started else 0
    if (age is None and since_start > 120) or (age is not None and age > 180):
        if runner_alive(d):
            return   # 心跳停了但运行器还在，例如电脑刚从睡眠醒来：不要把它当成丢失，否则 license 和核会被重复分配
        if not requeue_lost(j):
            finalize(cfg, j, "lost", T("运行器心跳中断（进程被杀或机器重启）",
                                       "runner heartbeat lost (process killed or machine rebooted)"))
        return
    max_h = j.get("max_h")
    if max_h and since_start > max_h * 3600 and not cancel:
        (d / "CANCEL").write_text("timeout", encoding="utf-8")
        dlog(T(f"{j['id']} 超过 max_h={max_h}，终止", f"{j['id']} exceeded max_h={max_h}; killing"))


def process_created_near(pid, started, tolerance_s=120):
    """防止 PID 被复用后误杀别的任务：进程创建时间要和记录的启动时间对得上。"""
    handle = _K32.OpenProcess(0x1000, 0, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        times = [ctypes.c_ulonglong() for _ in range(4)]
        if not _K32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return False
    finally:
        _K32.CloseHandle(handle)
    created = times[0].value / 1e7 - 11644473600        # FILETIME 转 Unix 时间
    return abs(created - started) <= tolerance_s


def runner_alive(jdir):
    info = read_json(pathlib.Path(jdir) / "runner.json")
    started = parse_iso((info or {}).get("started", ""))
    pid = (info or {}).get("runner_pid")
    return bool(pid and started and process_created_near(pid, started.timestamp()))


def kill_leftovers(jdir):
    info = read_json(pathlib.Path(jdir) / "runner.json")
    started = parse_iso((info or {}).get("started", ""))
    if not started:
        return
    for key in ("runner_pid", "child_pid"):
        pid = info.get(key)
        if pid and process_created_near(pid, started.timestamp()):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)


def requeue_lost(j):
    """声明了 --resume-script 的任务，丢失后用续跑脚本重新排队；次数用完返回 False。"""
    count = j.get("resume_count", 0)
    if not j.get("resume_argv") or count >= j.get("max_resume", 0):
        return False
    d = pathlib.Path(j["_dir"])
    kill_leftovers(d)
    for name in ("exit.json", "runner.json", "heartbeat"):
        (d / name).unlink(missing_ok=True)
    j["argv"] = j["resume_argv"]
    j.pop("cmd", None)
    j.pop("started", None)
    j["resume_count"] = count + 1
    j["status"] = "pending"
    save_job(j)
    dlog(T(f"{j['id']} 丢失，第 {count + 1} 次续跑", f"{j['id']} lost; resume #{count + 1}"))
    return True


def dep_state(j, by_id):
    """返回 (ok, 等待原因, 失败原因)"""
    for dep in j.get("after") or []:
        dj = by_id.get(dep)
        if dj is None:
            return False, None, T(f"依赖 {dep} 不存在", f"dependency {dep} does not exist")
        if dj["status"] in ACTIVE:
            return False, T(f"等待依赖 {dep}", f"waiting for dependency {dep}"), None
        if dj["status"] not in FINAL_OK:
            return False, None, T(f"依赖 {dep} 结果为 {dj['status']}", f"dependency {dep} ended {dj['status']}")
    return True, None, None


def start_job(cfg, j):
    j["status"] = "running"
    j["started"] = now_iso()
    j.pop("wait_reason", None)
    save_job(j)
    spawn_detached([windowless_python(), str(HERE / "simq.py"), "_run", j["_dir"]])
    dlog(T(f"{j['id']} 发车（{j['sw']}, {j['cores']} 核, {j['mem']} GB）",
           f"{j['id']} started ({j['sw']}, {j['cores']} cores, {j['mem']} GB)"))
    vote(cfg, "hold", f"simq {j['id']}", j.get("name", ""))


def set_wait(j, reason):
    if j.get("wait_reason") != reason:
        j["wait_reason"] = reason
        save_job(j)


def schedule(cfg, jobs):
    by_id = {j["id"]: j for j in jobs}
    running = [j for j in jobs if j["status"] == "running"]
    used_c = sum(j["cores"] for j in running)
    used_m = sum(j["mem"] for j in running)
    used_l = {}
    for j in running:
        used_l[j["sw"]] = used_l.get(j["sw"], 0) + 1
    lic = cfg.get("licenses", {})
    pend = [j for j in jobs if j["status"] == "pending"]
    pend.sort(key=lambda j: (-j["prio"], j["submitted"]))
    reserve_c = 0   # 为等待超时、卡在 license 上的任务预留的核数
    for j in pend:
        cancel = job_cancel_reason(j)
        if cancel:
            finalize(cfg, j, "cancelled", cancel)
            continue
        ok, wait, fail = dep_state(j, by_id)
        if fail:
            finalize(cfg, j, "failed", fail)
            continue
        if not ok:
            set_wait(j, wait)
            continue
        why = None
        lic_blocked = j["sw"] in lic and used_l.get(j["sw"], 0) >= lic[j["sw"]]
        if lic_blocked:
            why = T(f"{j['sw']} license 占满（{lic[j['sw']]}）", f"{j['sw']} license pool full ({lic[j['sw']]})")
        elif used_c + reserve_c + j["cores"] > cfg["cores"]:
            why = T(f"核数不够（已用 {used_c}/{cfg['cores']}" +
                    (f"，另为等待超时的任务预留 {reserve_c}" if reserve_c else "") + "）",
                    f"not enough cores (used {used_c}/{cfg['cores']}" +
                    (f", plus {reserve_c} reserved for a long-waiting job" if reserve_c else "") + ")")
        elif used_m + j["mem"] > cfg["mem_gb"]:
            why = T(f"内存预算不够（已分配 {used_m:g}/{cfg['mem_gb']} GB）",
                    f"memory budget full (allocated {used_m:g}/{cfg['mem_gb']} GB)")
        elif free_mem_gb() - j["mem"] < cfg["min_free_mem_gb"]:
            why = T(f"实际空闲内存不足（{free_mem_gb():.1f} GB）", f"not enough free memory ({free_mem_gb():.1f} GB)")
        if why is None:
            start_job(cfg, j)
            used_c += j["cores"]
            used_m += j["mem"]
            used_l[j["sw"]] = used_l.get(j["sw"], 0) + 1
            continue
        set_wait(j, why)
        waited = (now() - parse_iso(j["submitted"])).total_seconds() / 3600
        if waited > cfg.get("reserve_after_h", 2):
            if lic_blocked:
                # 卡在 license 上：要等同软件的某个在跑任务结束才能发车，那时至少释放其中最小一个的核。
                # 只需预留"释放后仍不够"的部分，其余空闲核可以给后面不占该 license 的任务用。
                held = min((r["cores"] for r in running if r["sw"] == j["sw"]), default=0)
                reserve_c = max(reserve_c, j["cores"] - held)
                continue
            break   # 卡在核数或内存上：给等太久的任务留位，后面的不再插队


def dispatcher_loop():
    import msvcrt
    lockf = open(DISP_LOCK, "a+")
    try:
        msvcrt.locking(lockf.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return   # 已有调度器在跑
    if DISP_STOP.exists():
        DISP_STOP.unlink()
    JOBS.mkdir(exist_ok=True)
    dlog(T(f"调度器启动 pid={os.getpid()}", f"dispatcher started pid={os.getpid()}"))
    queue_hold_at = None
    while True:
        cfg = load_config()
        try:
            jobs = all_jobs()
            for j in jobs:
                if j["status"] == "running":
                    update_running(cfg, j)
            schedule(cfg, jobs)
            pending = any(j["status"] == "pending" for j in all_jobs())
            if pending and (queue_hold_at is None or time.time() - queue_hold_at > 12 * 3600):
                vote(cfg, "done", "simq 队列")      # 先撤再登，刷新登记时间，避免 30 h 失效
                vote(cfg, "hold", "simq 队列", T("有任务在排队", "jobs are queued"))
                queue_hold_at = time.time()
            elif not pending and queue_hold_at is not None:
                vote(cfg, "done", "simq 队列")
                queue_hold_at = None
        except Exception as e:
            dlog(T(f"调度循环出错：{e!r}", f"dispatcher loop error: {e!r}"))
        write_json(DISP_FILE, {"pid": os.getpid(), "ts": time.time(), "beat": now_iso()})
        if DISP_STOP.exists():
            DISP_STOP.unlink()
            dlog(T("收到停止信号，调度器退出（在跑的任务不受影响）",
                   "stop signal received; dispatcher exiting (running jobs are not affected)"))
            if queue_hold_at is not None:
                vote(cfg, "done", "simq 队列")
            return
        time.sleep(cfg.get("poll_s", 10))


def dispatcher_alive():
    d = read_json(DISP_FILE, None)
    if not d:
        return False
    return time.time() - d.get("ts", 0) < 3 * load_config().get("poll_s", 10) + 15


def ensure_dispatcher(quiet=False):
    if dispatcher_alive():
        return True
    spawn_detached([windowless_python(), str(HERE / "simq.py"), "_dispatch"])
    for _ in range(20):
        time.sleep(0.5)
        if dispatcher_alive():
            if not quiet:
                print(T("（已启动后台调度器）", "(background dispatcher started)"))
            return True
    print(T("警告：调度器没能启动，看 dispatcher.log", "warning: dispatcher failed to start; see dispatcher.log"),
          file=sys.stderr)   # 不写 stdout：调用方从 stdout 读 job id
    return False


# ---------------------------------------------------------------- 命令行

def build_argv(script):
    p = pathlib.Path(script).resolve()
    if not p.exists():
        sys.exit(T(f"脚本不存在：{p}", f"script not found: {p}"))
    ext = p.suffix.lower()
    if ext == ".ps1":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)]
    if ext in (".bat", ".cmd"):
        return ["cmd", "/c", str(p)]
    if ext == ".py":
        return [console_python(), "-u", str(p)]
    sys.exit(T("--script 只支持 .ps1 / .bat / .cmd / .py", "--script supports only .ps1 / .bat / .cmd / .py"))


def check_preflight(cfg, a, cwd):
    """require_preflight 开启时，核对回执与脚本一致；返回写入 job.json 的回执摘要。"""
    if not cfg.get("require_preflight"):
        return None
    if a.smoke:
        limit = cfg.get("smoke_max_h", 0.5)
        if not a.max_h or a.max_h > limit:
            sys.exit(T(f"--smoke 必须同时给 --max-h，且不超过 {limit}",
                       f"--smoke needs --max-h as well, at most {limit}"))
        return {"smoke": True}
    if not a.script:
        sys.exit(T("已开启 require_preflight：正式任务必须用 --script，并先跑 sg.py preflight",
                   "require_preflight is on: real jobs must use --script and pass sg.py preflight first"))
    receipt = read_json(pathlib.Path(cwd) / ".simguard" / "receipt.json")
    if not receipt or receipt.get("status") != "PASS":
        sys.exit(T("没有通过的 preflight 回执：先跑 sg.py preflight <run_dir> --script <脚本>",
                   "no passing preflight receipt: run sg.py preflight <run_dir> --script <script> first"))
    if receipt.get("script_sha256") != sha256_of(a.script):
        sys.exit(T("脚本在 preflight 之后改过：请重新跑 preflight",
                   "script changed after preflight: run preflight again"))
    changed = []
    for rel, sha in (receipt.get("inputs") or {}).items():   # 驱动、判据、读回过的模型
        p = pathlib.Path(rel) if pathlib.Path(rel).is_absolute() else pathlib.Path(cwd) / rel
        if not p.is_file() or sha256_of(p) != sha:
            changed.append(rel)
    if changed:
        sys.exit(T(f"这些文件在 preflight 之后改过或不见了：{changed}；请重新跑 preflight",
                   f"these files changed or went missing after preflight: {changed}; run preflight again"))
    return {k: receipt.get(k) for k in ("created", "script_sha256", "gate_sha256")}


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_submit(a):
    cfg = load_config()
    if bool(a.cmd) == bool(a.script):
        sys.exit(T("--cmd 和 --script 必须给且只给一个", "give exactly one of --cmd and --script"))
    if a.prio is not None and not cfg.get("allow_prio_override", True):
        sys.exit(T("本机不允许用 --prio 插队（allow_prio_override=false）",
                   "--prio is not allowed on this machine (allow_prio_override=false)"))
    if a.cores > cfg["cores"] or a.mem > cfg["mem_gb"]:
        sys.exit(T(f"申请的资源超过总量（{cfg['cores']} 核 / {cfg['mem_gb']} GB），永远排不上",
                   f"request exceeds the total ({cfg['cores']} cores / {cfg['mem_gb']} GB); it would never start"))
    cwd = str(pathlib.Path(a.cwd or os.getcwd()).resolve())
    if not pathlib.Path(cwd).is_dir():
        sys.exit(T(f"cwd 不存在：{cwd}", f"cwd does not exist: {cwd}"))
    if a.script and not pathlib.Path(a.script).is_absolute():
        in_cwd = pathlib.Path(cwd, a.script)       # 相对路径先按 --cwd 解析，和 --resume-script 一致
        a.script = str(in_cwd if in_cwd.exists() else pathlib.Path(a.script).resolve())
    if a.script and not pathlib.Path(a.script).is_file():
        sys.exit(T(f"脚本不存在：{a.script}", f"script not found: {a.script}"))
    preflight = check_preflight(cfg, a, cwd)
    after = [resolve_id(x) for x in a.after.split(",") if x.strip()] if a.after else []
    jid = new_id(a.line)
    d = job_dir(jid)
    d.mkdir(parents=True)
    j = {"id": jid, "line": a.line, "name": a.name or jid, "sw": a.sw, "cores": a.cores, "mem": a.mem,
         "est_h": a.est, "max_h": a.max_h, "cwd": cwd, "after": after, "note": a.note or "",
         "prio": a.prio if a.prio is not None else cfg.get("line_priority", {}).get(a.line, 0),
         "status": "pending", "submitted": now_iso()}
    if a.resume_script:
        if not a.script:
            sys.exit(T("--resume-script 只能和 --script 一起用", "--resume-script only works with --script"))
        j["resume_argv"] = build_argv(pathlib.Path(cwd, a.resume_script))
        j["max_resume"] = a.max_resume
    if a.script:
        j["argv"] = build_argv(a.script)
        j["script"] = str(pathlib.Path(a.script).resolve())
    else:
        j["cmd"] = a.cmd
    if preflight:
        j["preflight"] = preflight
    write_json(d / "job.json", j)
    print(jid)
    ensure_dispatcher(quiet=True)


def row(j):
    st = j["status"]
    if st == "running":
        t = fmt_dur((now() - parse_iso(j["started"])).total_seconds())
        info = T(f"已跑 {t}", f"ran {t}") + \
            (T(f" / 预计 {j['est_h']:g}h", f" / est {j['est_h']:g}h") if j.get("est_h") else "")
    elif st == "pending":
        q = fmt_dur((now() - parse_iso(j['submitted'])).total_seconds())
        info = T(f"排队 {q}", f"queued {q}") + \
            (T(f"：{j['wait_reason']}", f": {j['wait_reason']}") if j.get("wait_reason") else "")
    else:
        s, e = parse_iso(j.get("started") or ""), parse_iso(j.get("ended") or "")
        info = (T(f"用时 {fmt_dur((e - s).total_seconds())}", f"took {fmt_dur((e - s).total_seconds())}")
                if s and e else "") + \
               (f" {j['reason']}" if j.get("reason") else "")
    return f"{j['id']:<28} {j['line'][:10]:<10} {j['name'][:28]:<28} {j['sw']:<9} {j['cores']:>3}c {j['mem']:>5g}G  {st:<9} {info}"


def cmd_list(a):
    jobs = all_jobs()
    if a.line:
        jobs = [j for j in jobs if j["line"] == a.line]
    if not a.all:
        cutoff = now() - datetime.timedelta(hours=24)
        jobs = [j for j in jobs if j["status"] in ACTIVE or (parse_iso(j.get("ended") or "") or now()) > cutoff]
    order = {"running": 0, "pending": 1}
    jobs.sort(key=lambda j: (order.get(j["status"], 2), -j["prio"] if j["status"] == "pending" else 0,
                             j.get("ended") or j["submitted"]))
    alive = dispatcher_alive()
    print(T(f"调度器：{'运行中' if alive else '未运行'}", f"dispatcher: {'running' if alive else 'not running'}"))
    if not alive and any(j["status"] in ACTIVE for j in jobs):
        print(T("警告：有未结束的任务，但调度器没在运行。运行 dispatcher start 恢复。",
                "warning: unfinished jobs exist but the dispatcher is not running; run `dispatcher start`."))
    if not jobs:
        print(T("（没有任务）", "(no jobs)"))
    for j in jobs:
        print(row(j))


def cmd_status(a):
    j = load_job(resolve_id(a.id))
    d = pathlib.Path(j["_dir"])
    print(row(j))
    for k in ("name", "line", "cwd", "cmd", "script", "after", "note", "prio", "submitted", "started", "ended", "exit_code", "reason"):
        if j.get(k) not in (None, "", []):
            print(f"  {k}: {j[k]}")
    print(T(f"  日志：{d / 'output.log'}", f"  log: {d / 'output.log'}"))
    tail_lines(d / "output.log", 8)


def tail_lines(path, n):
    try:
        data = pathlib.Path(path).read_bytes()[-200_000:]
    except FileNotFoundError:
        print(T("  （还没有输出）", "  (no output yet)"))
        return
    text = data.decode("utf-8", errors="replace")
    if text.count("\ufffd") > 20:
        text = data.decode("gbk", errors="replace")
    for line in text.splitlines()[-n:]:
        print("  | " + line)


def cmd_tail(a):
    j = load_job(resolve_id(a.id))
    tail_lines(pathlib.Path(j["_dir"]) / "output.log", a.n)


def cmd_wait(a):
    jid = resolve_id(a.id)
    deadline = time.time() + a.timeout
    while True:
        j = load_job(jid)
        if j["status"] not in ACTIVE or time.time() >= deadline:
            break
        time.sleep(min(10, max(1, deadline - time.time())))
    print(row(j))
    if j["status"] in ACTIVE:
        sys.exit(3)
    sys.exit(0 if j["status"] == "done" else 1)


def cmd_cancel(a):
    j = load_job(resolve_id(a.id))
    if j["status"] not in ACTIVE:
        print(T(f"{j['id']} 已是 {j['status']}，不用取消", f"{j['id']} is already {j['status']}; nothing to cancel"))
        return
    (pathlib.Path(j["_dir"]) / "CANCEL").write_text(a.reason, encoding="utf-8")
    print(T(f"已请求取消 {j['id']}（调度器下一轮处理）",
            f"cancel requested for {j['id']} (handled on the next dispatcher pass)"))
    ensure_dispatcher(quiet=True)


def cmd_res(a):
    cfg = load_config()
    running = [j for j in all_jobs() if j["status"] == "running"]
    uc, um, fm = sum(j['cores'] for j in running), sum(j['mem'] for j in running), free_mem_gb()
    print(T(f"核：{uc}/{cfg['cores']}   内存预算：{um:g}/{cfg['mem_gb']} GB   实际空闲：{fm:.1f} GB",
            f"cores: {uc}/{cfg['cores']}   memory budget: {um:g}/{cfg['mem_gb']} GB   free: {fm:.1f} GB"))
    for sw, n in cfg.get("licenses", {}).items():
        print(f"  {sw}: {sum(1 for j in running if j['sw'] == sw)}/{n}")


def cmd_dispatcher(a):
    if a.action == "start":
        print(T("调度器已在运行", "dispatcher already running") if dispatcher_alive() else "")
        ensure_dispatcher()
    elif a.action == "stop":
        DISP_STOP.write_text("stop", encoding="utf-8")
        print(T("已发停止信号；在跑的任务不受影响，下次 submit 或 start 会重新接管",
                "stop signal sent; running jobs are not affected, the next submit or start takes over again"))
    else:
        d = read_json(DISP_FILE, {})
        alive = dispatcher_alive()
        print(T(f"调度器：{'运行中' if alive else '未运行'}  pid={d.get('pid')}  最近心跳={d.get('beat')}",
                f"dispatcher: {'running' if alive else 'not running'}  pid={d.get('pid')}  last beat={d.get('beat')}"))


def cmd_health(a):
    cfg = load_config()
    jobs = [load_job(resolve_id(a.id))] if a.id else [j for j in all_jobs() if j["status"] == "running"]
    if not jobs:
        print(T("没有在跑的任务", "no running jobs"))
    stalled = False
    for j in jobs:
        d = pathlib.Path(j["_dir"])
        m = read_json(d / "metrics.json")
        if j["status"] != "running":
            print(f"{j['id']}  {j['status']}" + (f"  {j['reason']}" if j.get("reason") else "") +
                  (T(f"  内存峰值 {m['peak_rss_gb']:.1f}/{j['mem']:g} GB",
                     f"  peak memory {m['peak_rss_gb']:.1f}/{j['mem']:g} GB") if m else ""))
            continue
        verdict = health_verdict(m, cfg.get("stall_min", 20), time.time())
        stalled = stalled or verdict == "STALLED"
        if not m:
            print(T(f"{j['id']}  {verdict}  还没有采样", f"{j['id']}  {verdict}  no samples yet"))
            continue
        over = T("  ⚠ 超过声明内存", "  ⚠ exceeded declared memory") if m["peak_rss_gb"] > j["mem"] else ""
        cpu_ago, log_ago = fmt_dur(time.time() - m['cpu_changed_ts']), fmt_dur(time.time() - m['log_changed_ts'])
        print(T(f"{j['id']}  {verdict}  CPU {fmt_dur(m['cpu_s'])}（{cpu_ago}前增长）  "
                f"日志 {log_ago}前更新  "
                f"内存 {m['rss_gb']:.1f} GB，峰值 {m['peak_rss_gb']:.1f}/{j['mem']:g} GB{over}",
                f"{j['id']}  {verdict}  CPU {fmt_dur(m['cpu_s'])} (grew {cpu_ago} ago)  "
                f"log updated {log_ago} ago  "
                f"memory {m['rss_gb']:.1f} GB, peak {m['peak_rss_gb']:.1f}/{j['mem']:g} GB{over}"))
    sys.exit(4 if stalled else 0)


AUTOSTART_TASK = "simq-dispatcher"


def autostart_command():
    """登录时拉起调度器的计划任务。SIMQ_HOME 由 cmd 传给调度器。"""
    launch = f'"{windowless_python()}" "{HERE / "simq.py"}" _dispatch'
    if os.environ.get("SIMQ_HOME"):
        launch = f'cmd /c set "SIMQ_HOME={DATA}" && {launch}'
    return ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", AUTOSTART_TASK, "/TR", launch]


def cmd_autostart(a):
    if a.action == "print":
        print(subprocess.list2cmdline(autostart_command()))
        return
    argv = autostart_command() if a.action == "install" else \
        ["schtasks", "/Delete", "/F", "/TN", AUTOSTART_TASK] if a.action == "remove" else \
        ["schtasks", "/Query", "/TN", AUTOSTART_TASK]
    sys.exit(subprocess.call(argv))


def cmd_idle(a):
    busy = [j for j in all_jobs() if j["status"] in ACTIVE]
    print(T("空闲", "idle") if not busy else
          T(f"还有 {len(busy)} 个任务排队或在跑", f"{len(busy)} job(s) still queued or running"))
    sys.exit(0 if not busy else 1)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "_run":
        return runner(sys.argv[2])
    if len(sys.argv) >= 2 and sys.argv[1] == "_dispatch":
        return dispatcher_loop()
    ap = argparse.ArgumentParser(prog="simq", description=T("单机共享的仿真队列", "Shared single-machine simulation queue"))
    sub = ap.add_subparsers(dest="cmd_name", required=True)

    s = sub.add_parser("submit", help=T("提交任务", "submit a job"))
    s.add_argument("--line", required=True, help=T("所属研究线（自由命名）", "research line (free-form name)"))
    s.add_argument("--name", help=T("简短任务名", "short job name"))
    s.add_argument("--sw", required=True, choices=SW_CHOICES, help=T("占用哪种 license", "which license the job uses"))
    s.add_argument("--cores", type=int, required=True)
    s.add_argument("--mem", type=float, required=True, help=T("峰值内存估计（GB）", "estimated peak memory (GB)"))
    s.add_argument("--est", type=float, help=T("预计时长（小时），只用于显示", "estimated runtime (hours), display only"))
    s.add_argument("--max-h", dest="max_h", type=float, help=T("超过这么多小时强制终止（默认不限）", "kill after this many hours (default: no limit)"))
    s.add_argument("--cwd")
    s.add_argument("--cmd", help=T("cmd.exe 下执行的命令行", "command line run under cmd.exe"))
    s.add_argument("--script", help=T(".ps1/.bat/.cmd/.py 脚本", ".ps1/.bat/.cmd/.py script"))
    s.add_argument("--after", help=T("依赖的任务 id，逗号分隔；全部 done 才发车", "comma-separated job ids; starts only when all are done"))
    s.add_argument("--prio", type=int, help=T("优先级，默认取 config 里该 line 的值", "priority; default is the line's value in config"))
    s.add_argument("--note")
    s.add_argument("--smoke", action="store_true", help=T("短测试，免 preflight 回执，但必须给 --max-h", "short test: no preflight receipt needed, but --max-h is required"))
    s.add_argument("--resume-script", help=T("丢失后用这个脚本自动续跑；相对路径按 --cwd 解析", "script to auto-resume a lost job; relative to --cwd"))
    s.add_argument("--max-resume", type=int, default=2, help=T("最多自动续跑几次", "max automatic resumes"))
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("list", help=T("列出任务", "list jobs"))
    s.add_argument("--all", action="store_true")
    s.add_argument("--line")
    s.set_defaults(fn=cmd_list)

    for name, fn in (("status", cmd_status), ("tail", cmd_tail), ("wait", cmd_wait), ("cancel", cmd_cancel)):
        s = sub.add_parser(name)
        s.add_argument("id")
        if name == "tail":
            s.add_argument("-n", type=int, default=40)
        if name == "wait":
            s.add_argument("--timeout", type=float, default=540)
        if name == "cancel":
            s.add_argument("--reason", default="user")
        s.set_defaults(fn=fn)

    sub.add_parser("res", help=T("资源占用", "resource usage")).set_defaults(fn=cmd_res)
    s = sub.add_parser("health", help=T("判断任务是否在推进；退出码 4 表示有任务停滞", "check whether jobs are progressing; exit code 4 means a job is stalled"))
    s.add_argument("id", nargs="?")
    s.set_defaults(fn=cmd_health)
    s = sub.add_parser("dispatcher")
    s.add_argument("action", nargs="?", default="status", choices=["start", "stop", "status"])
    s.set_defaults(fn=cmd_dispatcher)
    s = sub.add_parser("autostart", help=T("登录时自动拉起调度器（Windows 计划任务）", "start the dispatcher at logon (Windows scheduled task)"))
    s.add_argument("action", choices=["print", "install", "remove", "status"])
    s.set_defaults(fn=cmd_autostart)
    sub.add_parser("idle").set_defaults(fn=cmd_idle)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
