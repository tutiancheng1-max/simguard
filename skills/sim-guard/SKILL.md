---
name: sim-guard
description: 让 agent 安全地规划、提交和报告长时间科学仿真：限定工作范围，通过 simq 排队，提交前读回已保存的模型，冻结判据，要求独立校验和可追溯的报告。只要任务涉及运行、派子 agent 跑、续跑或汇报 COMSOL、Lumerical FDTD/MODE、Fluent、MATLAB 或其他长时间求解，或者要写仿真驱动脚本、runbook 和参数扫描，就使用本 skill，即使用户没有提到它。纯文献阅读、与求解无关的画图和改文字不需要。
---

# sim-guard

长时间仿真最贵的错误，是模型设错却仍然算得出看似合理的结果，还通过了校准。本 skill 的流程就是为了尽早抓住这类错误。它的检查写在脚本里：文字可能被忽略，脚本的结果不会。

下文的 `sg.py` 指本 skill 的 `scripts/sg.py`，用它的绝对路径调用。

## 开始前

1. 确认能找到 `simq.py`（`--simq`、环境变量 `SIMQ_PY`，或项目 `simguard.project.json` 的 `simq_py`），然后运行 `python <simq.py> dispatcher status`。
2. 找不到就停下并告诉用户，指向 repo 的 README。不要改为直接启动求解器：其他研究线可能在用同一台机器和 license。
3. 派子 agent 时，先读 [references/agent-brief.md](references/agent-brief.md)，把填好的模板放进它的 prompt。子 agent 不一定会读到本 skill。

## 工作范围

除非用户明确授权：

- 只写本次的 run 目录。旧的 run 目录、已有结果和其他研究线都只读。
- 不改论文和投稿材料，不发邮件，不提交论文，不推送到远程。
- 不改已冻结的判据、预测和参数。
- 不用 `--prio` 插队，不改 `simq` 的配置或代码，不替队列登记关机，也不自行关机。
- 不删除文件。

超出范围但值得做的事，记下来报告给用户。

## 流程

### 1. 建立 run 目录

```
python sg.py init <run_dir>
```

填写 `manifest.json` 和 `gate.json`。一次运行只回答一个问题。

### 2. 先冻结判据，再看结果

在 `gate.json` 里写通过标准和独立校验方式，然后运行：

```
python sg.py freeze <run_dir>
```

先定判据，才能避免看到结果后不自觉地调整标准。如果判据确实写错了，向用户说明，得到同意后用 `freeze --amend "原因"` 更新，历史会保留。

独立校验的选择见 [references/validation.md](references/validation.md)。“和旧结果一致”不算：旧结果可能带着同一个错误。

### 3. 写驱动脚本

- 优先复制本 skill 的 `assets/templates/<求解器>/`，它们把建模和求解分成两个阶段。其他来源要来自 `approved_templates` 或干净的来源，并记入 `derived_from`。带有 `DO_NOT_REUSE` 标记的目录不能复用。
- 按求解器读取注意事项：[references/comsol.md](references/comsol.md)、[references/lumerical.md](references/lumerical.md)。
- 脚本读取 `SIMQ_CORES` 设定线程数。长任务要能续跑。
- 用文献公式前，先复现原文的一张图或一个数，记入 `manifest.literature`。原文公式也可能和它自己的图不一致。
- 关闭某项物理效应时，用测试确认它真的关了，记入 `manifest.switch_tests`。参数名可能和实际行为不符。
- 内存先用小测试实测，再写入 `resources`。内存可能随线程数增长，线程多不一定更快。

### 4. 读回已保存的模型

把模型保存后再检查保存下来的文件。建模代码看起来对，不代表模型里的值对。把模型写入 `manifest.readback`，由 `preflight` 核对。

所以建模阶段要先于 `sg.py submit` 运行：用 `simq submit --script build.py ... --max-h 0.5` 提交；开启了 `require_preflight` 的队列再加 `--smoke`。几分钟内的调试也可以用交互式 MCP。

任务显示 `done` 只表示退出码为 0，不代表求解成功。有些求解器出错时退出码也是 0，所以脚本要检查日志和产物，并在写报告前确认结果文件来自这一次运行。

### 5. 通过检查后提交

```
python sg.py submit <run_dir> --script run.ps1 --sw comsol --est 3
```

`--script` 的相对路径按 run 目录解析。它会先跑全部检查，通过后才调用 `simq`，并把 job id 记入 manifest。其余参数原样传给 `simq`，例如 `--after <id>`、`--max-h 10`。

检查不通过时要修复问题。不要改清单来迎合检查，也不要绕开 `sg.py`。确实是误报时，向用户说明；已知安全的写法可以加行内注释 `simguard: allow <规则 id>`，并写明原因。

提交后可以结束或做别的，求解进程不依赖你。回来时用 `simq status`、`tail`、`health`，或 `wait <id> --timeout 540`。判断任务是否卡住要用 `simq health`，不要只看日志。任务失败时，用 `sg.py diagnose <job id>` 分类，再读日志确认。状态 `lost` 表示运行器中断，需要重新提交；提交时给了 `--resume-script` 的任务会自动续跑。

多个算例或多个阶段时，用 `scripts/sweep.py`，先读 [references/sweep.md](references/sweep.md)。

### 6. 写报告

```
python sg.py report <run_dir>
```

先按模板填完 `REPORT.md` 的每一段再运行：`判据结果` 表每条判据一行，`检查` 表每项都给状态和证据。没做的检查就写 `NOT_DONE`：报告的价值在于让读者知道哪些还没验证。必查项没做时，脚本不接受 `Overall: PASS`。

## 遇到决定时

- 可撤回的技术选择，例如网格加密顺序：按你的推荐继续，写进报告的“按推荐执行的决定”，并告诉用户。不要让机器空等。
- 改判据、冻结的预测或参数，以及扩大范围、删除文件、对外发送：停下来等用户。
- 求解器或数值失败：先诊断，同一问题最多重试 2 次，然后如实报告。慢本身不算故障。
- 结果不通过：写 `FAIL` 并报告，不要调参数直到通过。

## 发现错误后

如果发现已有结果可能受错误影响：

1. 不要静默修复。告诉用户。
2. 用 `provenance.json` 和搜索列出受影响的 run 和数字。
3. 把错误写法加进项目的 `forbidden_patterns`，在受影响的目录放 `DO_NOT_REUSE` 说明。这样同一个错误不会第二次发生。
