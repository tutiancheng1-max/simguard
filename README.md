# simguard

让 AI agent 跑长时间科学仿真时，少犯那些“算得出结果、但模型其实设错了”的错误。

`simguard` 由两部分组成：

- **`simq`**：单机仿真队列。多个对话和子 agent 共用一台机器时，按核数、内存和 license 排队，求解进程不依赖提交它的 agent。
- **`sim-guard` skill**：给 Claude Code 用的流程和检查脚本。它限定 agent 的工作范围，在提交前读回已保存的模型，冻结判据，并要求报告写明没做的检查。

**English summary:** a single-machine solver queue plus a Claude Code skill that makes agents read back saved models, freeze pass/fail criteria before running, use independent validation, and report what was not checked. The docs are in Chinese.

## 它解决什么问题

| 问题 | 机制 |
|---|---|
| 张量只设了一个分量，模型照常能算 | 不开求解器，直接读回 `.mph` 并核对 |
| 校准通过，但基准和模型带同一个错误 | 判据里不接受“和旧结果一致”作为校验 |
| 新脚本从有 bug 的旧脚本复制而来 | 搜索已知错误写法，拒绝带不可复用标记的来源 |
| 看到结果后调整通过标准 | 判据先冻结并记录哈希，修改留下历史 |
| 参数名写着关闭，效果却没关 | 开关测试不通过就不能提交 |
| 报告写通过，但网格收敛没做 | 必查项没做时，不接受 `Overall: PASS` |
| 子 agent 越界、插队或绕过排队 | 工作范围清单、任务模板、队列端回执检查 |
| 求解器出错，退出码却是 0 | 查日志标记和产物，不把它记成成功 |
| 扫描的所有算例其实用了同一个参数 | 每个算例读回自己的参数 |
| 分不清任务是慢还是卡死 | 监控进程树的 CPU、内存和日志 |

## 依赖

- Windows（`simq` 使用 Windows 的进程和内存接口；`sg.py` 跨平台，但目前只在 Windows 上测过）
- Python 3.9 以上，无第三方依赖
- [Claude Code](https://claude.com/claude-code)，用于加载 skill
- 你自己的求解器和 license，本 repo 不包含任何求解器

## 安装

**1. 配置队列**

```powershell
Copy-Item simq\config.example.json simq\config.json
```

把 `simq\config.json` 里的核数、内存和 license 数改成你的机器。`require_preflight` 设为 `true` 时，正式任务必须先通过检查。

**2. 安装 skill**

```powershell
Copy-Item -Recurse skills\sim-guard $HOME\.claude\skills\sim-guard
```

**3. 告诉 skill 队列在哪里**

设置环境变量 `SIMQ_PY` 为 `simq.py` 的绝对路径；或者在项目根目录放一个 `simguard.project.json`，写上 `simq_py`。

**4. 配置项目规则（可选）**

参考 [simguard.project.example.json](skills/sim-guard/assets/simguard.project.example.json)：

| 字段 | 作用 |
|---|---|
| `simq_py` | `simq.py` 的路径，相对于配置文件 |
| `readonly_paths` | run 目录不能放在这些路径下 |
| `approved_templates` | 设置后，`derived_from` 只能来自这些路径 |
| `do_not_reuse_markers` | 目录里有这些文件名时，不能复用其中的脚本 |
| `forbidden_patterns` | 项目已知的错误写法 |
| `comsol_tensor` | 要检查哪些物理场的张量 |
| `required_report_checks` | 报告必须多回答哪些检查 |

**5. 把规则写进项目的 CLAUDE.md（推荐）**

```markdown
长时间求解一律走 sim-guard 流程和 simq 队列，不要直接启动求解器。
派子 agent 时，把 sim-guard 的 references/agent-brief.md 填好放进 prompt。
```

## 用法

在 Claude Code 里提出仿真任务，skill 会自动触发。agent 走的流程如下：

```powershell
python sg.py init runs\case1
# 填写 gate.json 和 manifest.json
python sg.py freeze runs\case1
python sg.py submit runs\case1 --script run.ps1 --sw comsol --est 3
# 求解完成，填写 REPORT.md
python sg.py report runs\case1
```

你也可以手动检查已有的文件：

```powershell
python sg.py readback model.mph
python sg.py scan path\to\drivers
```

## 端到端测试

[examples/e2e.py](examples/e2e.py) 用真实求解器跑很短的算例：正确版本要走通，故意设错的版本要被拦下。它会占用 license，请在机器空闲时通过队列运行：

```powershell
python examples\e2e.py comsol --workdir D:\e2e\comsol --simq C:\path\simq.py
python examples\e2e.py lumerical --workdir D:\e2e\lumerical --simq C:\path\simq.py
```

## 参数扫描

多个算例或多个阶段时用 `sweep.py`，说明见 [references/sweep.md](skills/sim-guard/references/sweep.md)：

```powershell
python sweep.py plan sweep.json
python sweep.py advance sweep.json --wait 540
python sweep.py collect sweep.json
```

重复运行不会重复提交。每个算例都会读回自己的参数；license 问题和丢失的任务按上限重试，其他失败会停下，不影响别的算例。

## 能力边界

这些检查能减少错误，但不能证明模型正确。

- 张量读回只自动覆盖 COMSOL 系数型 PDE 的 `c`。其他设置和其他求解器，要由 agent 导出 JSON 并配置核对项。
- `manifest`、`gate` 和报告里的内容由 agent 填写。开关测试是否真的做过、`shares_code_with_model` 是否属实、报告证据是否可靠，脚本只能看到记录，需要人或另一个 agent 复核。
- `switch_tests` 和 `literature` 留空时，脚本视为没有相关项。
- 回执是普通文件，这些检查防的是疏忽和偷懒，不防故意伪造。
- 判据冻结只防止运行后被修改，不能保证判据本身合理。
- 除非加 hook，否则 agent 仍有可能直接启动求解器。
- 失败分类只看日志文字，可能分错。`STALLED` 也只是提示，不代表任务一定卡死。
- `sweep.py advance` 要有人或 agent 反复运行才会推进下一阶段，它不是常驻服务。

## 测试

```powershell
python -m unittest discover -s tests
```

测试会用临时目录启动一个独立的调度器，不碰已有队列，也不调用任何求解器。`autostart` 只测试 `print`，不会真的创建计划任务。

## 许可证

[MIT](LICENSE)
