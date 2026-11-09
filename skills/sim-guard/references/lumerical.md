# Lumerical 注意事项

## 模板

从 `assets/templates/lumerical/` 开始：`run_case.py` 写物理，`build.py` 和 `solve.py` 是提交给 simq 的两个阶段，`build` 会导出 `readback.json`。把整个目录复制到 run 目录，先跑通，再改物理。

lumapi 需要 numpy。系统 Python 没有时，模板会切换到 Lumerical 自带的 Python。

## 提交

- 调用 `lumapi` 的 Python 脚本也要用 `--sw lumerical`，不能用 `--sw python`，否则绕过 license 排队。`preflight` 会检查。
- 交互式 pylumerical 会话只用来建模和短测试。正式计算导出 `.fsp` 或写成脚本后提交。
- `switchtolayout()` 会清掉结果。`run()` 可能原地覆盖工程文件。需要保留时，先另存。

## 读回

没有内置的 `.fsp` 解析。用 `lumapi` 打开已保存的文件，把关键设置导出成 JSON，放在 run 目录，再让 `preflight` 核对：

```json
"readback": {
  "sources": {"rb": "readback.json"},
  "expect": [
    {"select": "rb:source.wavelength_center_m", "op": "approx", "value": 1.55e-6, "rel": 1e-6},
    {"select": "rb:fdtd.boundary_*", "op": "one_of", "value": ["PML", "Periodic"]},
    {"select": "rb:material.index_diag", "op": "isotropic"}
  ]
}
```

导出时，张量写成 1、4 或 9 个数的列表，就能用张量比较方式。旧写法 `dump` 加 `checks` 仍然可用。

值从 `gate.json` 或设计文档来，不要从模型里抄。常见要核对的设置：

- 光源的波长、偏振、方向和模式编号。
- 材料模型和拟合范围。
- 边界条件和对称面。对称面要和光源、结构的对称性相符。
- 网格覆盖区和仿真时间。
- 监视器的位置、法向和频点。

## 收敛和校验

- 确认场在仿真结束前已经衰减，不是到时间上限才停。
- 要加密网格、移动 PML 并比较结果。金属尖端和细缝通常需要局部加密。
- 能量闭合：透射、反射和吸收之和应接近 1。
- 伴随优化得到的结构，最后要用独立的前向仿真重新验证，并检查制造公差。
- 色散材料拟合要覆盖整个带宽，并查看拟合误差。
