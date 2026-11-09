# simq：单机仿真队列

多条研究线共用一台 Windows 机器时，所有**长时间的求解**都通过 simq 提交，由后台调度器按核数、内存和 license 统一发车。
提交任务的 agent 不必守着：求解进程与 agent、与对话的寿命无关。

```
python simq.py <命令>
```

配置、任务和日志默认放在 `simq.py` 同目录；设置环境变量 `SIMQ_HOME` 可以放到别处。

## 给 agent 的规则

1. **不要直接起求解器**跑长任务，包括 comsolbatch、fdtd-engine、fluent -g、matlab -batch 等。一律 `submit`。
   - 交互式会话只用来建模、调试、做几分钟内的短测试。
   - 正式计算请导出模型文件，写成脚本后提交。
2. **用 `--script` 提交，不用 `--cmd`**。PowerShell 5.1 会把嵌套的引号弄坏。
   - 脚本可以读取环境变量 `SIMQ_CORES`，例如 `comsolbatch -np $env:SIMQ_CORES ...`。
3. 资源要**如实估峰值**：`--cores` 是给求解器的线程数，`--mem` 是峰值 GB。
   - 估小了，调度器会把几个任务同时塞进来，把内存撑爆。
   - 估大了，只会多排一会儿队。
4. 调用求解器的 Python 脚本，`--sw` 要写求解器名，不能写 `python`，否则绕过 license 排队。
5. 提交后记下 job id，然后可以结束或去做别的。回来时用 `status`、`tail` 或 `wait`。
6. 前后有依赖的步骤用 `--after <id>`。前一个任务不是 done，后一个会自动判为 failed。

## 命令

| 命令 | 作用 |
|---|---|
| `submit --line L --name N --sw comsol --cores 16 --mem 30 --est 3 --cwd D --script run.ps1` | 提交任务，输出 job id |
| `list [--all] [--line L]` | 列出排队中、运行中，以及近 24 h 结束的任务，并显示等待原因 |
| `status <id>` / `tail <id> [-n 80]` | 详情和日志尾部（日志在 `jobs/<id>/output.log`） |
| `wait <id> [--timeout 540]` | 阻塞等待。退出码：0 = done，1 = 失败、取消或丢失，3 = 仍在跑 |
| `cancel <id>` | 取消任务，连同子进程树一起杀掉 |
| `health [id]` | 进程树的 CPU、内存和日志更新；判断 PROGRESSING 或 STALLED，有停滞时退出码为 4 |
| `autostart print\|install\|remove\|status` | 登录时拉起调度器的计划任务 |
| `res` | 核数、内存、license 的占用情况 |
| `dispatcher [start\|stop\|status]` | 调度器。submit 时会自动拉起；stop 不影响在跑的任务 |
| `idle` | 队列空且没有任务在跑时，退出码为 0 |

`--script` 的相对路径先按 `--cwd` 解析。`submit` 的标准输出只有 job id，警告写到标准错误，调用方可以放心解析。

`<id>` 写能唯一识别的一段即可。可选参数：`--max-h`（超过这么多小时强制终止）、`--prio`、`--note`、`--smoke`、`--resume-script`、`--max-resume`。

`--sw` 可选 comsol / lumerical / fluent / matlab / python / other。只有在 `licenses` 里配置的软件才占 license。

## 重启恢复

调度器默认只在 `submit` 或 `dispatcher start` 时拉起，机器重启后排队任务不会自己开始。

- `python simq.py autostart install`：创建登录时启动调度器的计划任务。这会改系统设置，请自己决定是否安装。`print` 只显示命令，`remove` 删除，`status` 查看。
- `submit --resume-script resume.ps1 --max-resume 2`：任务丢失后，调度器清理残留进程，再用续跑脚本重新排队。脚本里可以读取 `SIMQ_RESUME_COUNT`。不写这个参数时，任务停在 `lost`，需要手动处理。
- 续跑脚本要真的从保存点继续，否则只是重算一遍。
- `simq` 的 `done` 只表示退出码为 0。有些求解器出错时退出码也是 0，见下一节。

## `done` 不等于成功

`comsolbatch` 在 Java 出错时退出码可能仍是 0。队列会查 `error_markers` 列出的日志文字，查到就判为 `failed`；这只能发现已知标记，所以脚本仍要检查日志和产物，出错时返回非 0。sim-guard 的模板已经这样做。

## 配置（config.json）

调度器每一轮都会重新读取。

| 键 | 作用 |
|---|---|
| `cores`、`mem_gb` | 可分配的总核数和总内存 |
| `min_free_mem_gb` | 发车后实际空闲内存至少剩这么多 |
| `licenses` | 每种软件的 license 数 |
| `line_priority` | 研究线优先级，数字大的先跑 |
| `reserve_after_h` | 排在前面的任务等待多久后，开始为它预留资源 |
| `require_preflight` | `true` 时，正式任务必须带 sim-guard 的通过回执，且回执覆盖的文件（脚本、驱动、判据、读回过的模型）都没改动 |
| `smoke_max_h` | `--smoke` 免回执，但必须给不超过这个值的 `--max-h` |
| `allow_prio_override` | `false` 时不允许用 `--prio` 插队 |
| `stall_min` | CPU 和日志都这么多分钟没变化时，`health` 判为 STALLED。只报告，不杀任务 |
| `mem_guard` | `true` 时，任务超过声明内存 10% 且系统空闲内存低于 `min_free_mem_gb` 的一半，就终止这个任务。默认 `false` |
| `error_markers` | 退出码为 0 但日志里有这些文字时，判为 `failed` |
| `shutdown_vote` | 可选的外部关机守卫脚本，参数为 `hold\|done <名字> <备注>` |

## 调度规则

- 按优先级排序，同优先级先到先跑。
- 同时满足以下条件才发车：核数和内存预算够、该软件的 license 有空位、实际空闲内存减去本任务后仍够。
- 排在前面的任务资源不够时，后面的小任务可以先跑。前面的任务等满 `reserve_after_h` 后：
  - 如果它卡在核数或内存上，后面的任务不再插队；
  - 如果它卡在 license 上，后面不占这个 license 的任务仍可用空闲的核，只预留同软件任务结束后它仍缺的核数。
- 任务状态有：pending、running、done、failed、cancelled、timeout、lost（运行器进程不在了，例如重启，需要重新提交）。
- 心跳超时但运行器进程还在时（例如电脑刚从睡眠醒来），任务保持 running，不会被判为 lost，它占的 license 和核也不会被重复分配。
