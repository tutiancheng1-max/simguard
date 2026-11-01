# 子 agent 任务模板

子 agent 不一定会读到 CLAUDE.md 或本 skill。复制下面的内容，填好后放进它的 prompt。

```text
任务：<一句话；要回答的问题>
run 目录：<绝对路径>。只能写这个目录。
只读：<旧 run 目录、其他研究线>
求解器和 --sw：<comsol / lumerical / ...>。调用 lumapi 的 Python 脚本也用 --sw lumerical。
模板来源：<干净模板的路径；不要从别的旧驱动复制>

规则：
1. 长求解一律走 simq，规则见 <simq README 的绝对路径>。不要直接启动求解器。
   交互式 MCP 只用于建模、调试和几分钟内的短测试。
2. 用 <sg.py 的绝对路径> 按顺序执行：init、填写、freeze、preflight 或 submit、写报告后 report。
   流程说明见 <SKILL.md 的绝对路径>。
3. preflight 不通过就修复问题，不要绕过它，也不要为了通过而改清单。
4. 判据冻结后不能改。结果不通过就如实写 FAIL。
5. 不改论文，不删文件，不改 simq 配置，不用 --prio，不关机，不对外发送任何内容。
6. 可撤回的技术选择按你的推荐继续并记录；改判据或扩大范围时停下来报告。

结束时报告：Overall 状态、REPORT.md 路径、job id、没做的检查，以及任何偏离计划的地方。
```

派出后，用 `sg.py report <run_dir>` 和 `simq status <id>` 核实，不要只听子 agent 的总结。
