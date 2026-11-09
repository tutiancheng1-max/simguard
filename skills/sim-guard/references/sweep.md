# 参数扫描和任务链

多个算例或多个阶段时，用 `scripts/sweep.py`，不要手写循环提交。它会记住状态：重复运行不会重复提交，已完成的算例不会重算，只推进没做完的部分。

## 准备

```
扫描目录/
├── sweep.json          参考 assets/sweep.example.json
└── template/           一个已经跑通的算例目录
    ├── gate.json       判据，展开前必须填好
    ├── manifest.json   run_id、line、solver 会自动填写
    ├── build.py、solve.py ...
```

- 先用单个算例跑通模板，再扫描。否则错误会被放大几十倍。
- 脚本从 `params.json` 读取本算例的参数。COMSOL 和 Lumerical 模板已经支持。
- 每个阶段都要写 `outputs`。任务显示 `done` 时，这些文件必须存在且是这次写的，否则算例会停下。
- 需要读回才能开始的求解阶段写 `"guarded": true`，它前面的建模阶段不写。不是 guarded 的阶段按短测试提交，必须写 `max_h`，且不超过 simq 的 `smoke_max_h`（默认 0.5）。
- 要让 `collect` 自动判定，给 `gate.json` 的判据加 `result_key`，对应 `result.json` 里的键。

## 用读回确认参数真的生效

扫描最隐蔽的错误是所有算例其实用了同一个参数。在模板的 `manifest.json` 里用 `{"$param": "名字"}` 引用取值，每个算例会核对自己的值：

```json
"readback": {
  "sources": {"model": "prepared.mph"},
  "expect": [{"select": "model:param/k", "op": "equals", "value": {"$param": "k"}}]
}
```

Lumerical 读导出的 JSON：

```json
{"select": "rb:slab.index", "op": "approx", "value": {"$param": "index"}, "rel": 1e-6}
```

`collect` 还会做一次兜底检查：参数取值不同、结果却完全相同时，退出码为 1 并列出这些结果键。

## 运行

```
python sweep.py plan sweep.json           # 展开算例，不提交；先看算例数和预计时长
python sweep.py advance sweep.json --wait 540
python sweep.py status sweep.json
python sweep.py collect sweep.json
```

`advance` 的退出码：0 全部完成，1 有算例停下等人处理，3 还有任务未结束。退出码是 3 时再运行一次即可。可以先结束对话，之后回来继续：任务由 simq 运行，但下一阶段要靠 `advance` 提交，没人运行它时不会推进。

同一个扫描同时只能运行一个 `plan`、`advance` 或 `retry`，第二个会直接报错退出。每处理完一个算例就保存状态，中途被打断也不会重复提交。

## 失败时

| 原因 | 自动处理 |
|---|---|
| `license`、`lost` | 按 `max_retry` 重试 |
| `memory` | 停下：减少线程或规模，更新内存估计 |
| `convergence` | 停下：先诊断，同一问题最多重试 2 次 |
| `config`、`preflight` | 停下：修复脚本或模型 |
| `false_done` | 停下：显示 done，但没有这次的产物，或 JSON 产物读不出、含 NaN |
| `error` | 停下：这个算例的文件坏了，例如 manifest 不是合法 JSON；其他算例照常推进 |

停下的算例不影响其他算例。修复后运行 `sweep.py retry sweep.json <算例 id>`，再运行 `advance`。如果修复的是物理设置，要重新开一个扫描目录，不要混用不同版本的结果。

`classify` 只按日志文字判断，可能分错。`sg.py diagnose <job id>` 给的只是起点，要读日志确认。

## 监控单个任务

`simq health [id]` 查看进程树的 CPU 时间、内存和日志更新：

- `PROGRESSING`：CPU 或日志有变化。直接求解器分解时可能很久不写日志，这是正常的，慢不算问题。
- `STALLED`：两者都超过 `stall_min` 分钟没变化，退出码 4。只报告，不会杀任务。先用 `tail` 看日志再决定。
