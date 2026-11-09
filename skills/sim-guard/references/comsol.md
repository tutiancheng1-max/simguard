# COMSOL 注意事项

## 模板

从 `assets/templates/comsol/` 开始：`CaseDriver.java` 写物理，`build.py` 和 `solve.py` 是提交给 simq 的两个阶段。把整个目录复制到 run 目录，先跑通，再改物理。

- `comsolbatch` 在 Java 出错时退出码可能是 0。`run_case.py` 会检查日志和产物，出错时返回非 0。不要去掉这段检查。
- 默认的安全设置不允许 Java 读环境变量，所以路径和阶段写在生成的 `CaseConfig.java` 里。
- `println` 只出现在标准输出，不在 `-batchlog` 里。模板用 `SG_RESULT|` 行传结果。
- 第一次求解会自动生成数据集 `dset1`，不要再手动创建。

## 张量

系数型 PDE 的扩散系数 `c` 是张量，默认值不是 0。只设置一个分量时，其他分量保留默认值，模型照常能算，结果还可能看起来合理。

```java
// 错：只设了 c11，其余对角分量仍是 1
feature("cfeq1").setIndex("c", expr, 0);

// 对：写完整张量（2D 轴对称是 4 个分量）
feature("cfeq1").set("c", new String[]{expr, "0[W/m/K]", "0[W/m/K]", expr});
```

`sg.py scan` 会搜索这种写法。

## 读回

`.mph` 是 zip 文件，根目录的 `dmodel.xml` 存着模型设置。`sg.py readback` 直接读它，不需要 COMSOL，也不占 license。

先列出模型里有哪些值：

```
python sg.py readback models/prepared.mph --list
```

键的形式：

| 键 | 内容 |
|---|---|
| `param/<名>` | 全局参数，例如 `param/lambda0 = '2250[nm]'` |
| `physics/<物理场>/<特征>/<参数>` | 物理场设置，例如 `physics/pde/cfeq1/c` |
| `material/<材料>/<属性组>/<参数>` | 材料属性，例如 `material/W/def/relpermittivity` |

张量是 1、4 或 9 个分量的列表。然后在 `manifest.json` 里写期望：

```json
"readback": {
  "sources": {"model": "models/prepared.mph"},
  "expect": [
    {"select": "model:physics/*/*/c", "op": "isotropic", "physics_op": ["CoefficientFormPDE"]},
    {"select": "model:param/k", "op": "equals", "value": 2},
    {"select": "model:material/*/*/thermalconductivity", "op": "nonzero"}
  ],
  "allow": [{"key": "model:physics/pde2/cfeq1/c", "reason": "这里确实是各向异性"}]
}
```

- 不写任何期望时，内置的 `no_partial_default` 仍会检查所有张量（物理场和材料）：对角线上一部分是默认值 1、另一部分不是，就判 FAIL。它不需要知道物理场的名字。
- `physics_op` 按物理场类型筛选，例如 `CoefficientFormPDE`、`HeatTransfer`。
- `select` 没匹配到值时判 FAIL。通常是键写错了，不能当作没有问题。
- 内置传热接口的导热系数在材料里（`material/.../thermalconductivity`），不在物理场里。
- 还要核对运行时真正使用的模型，不只是建模阶段保存的副本。
- 轴对称模型要确认 `r` 权重写进了 `c`、`a`、`f`、`da` 和界面通量。
- 旧写法 `comsol_tensor_models`、`comsol_params`、`anisotropic_ok` 仍然可用，会被自动转换。

## 运行

- 用 `comsolbatch -np $env:SIMQ_CORES`，通过 `simq` 提交。
- 内存可能随线程数增长。先用小模型测两个线程数；内存不够时试着减少线程，不要一味加线程。
- 大模型的直接求解器分解时可能很久不写日志。判断是否停滞要看 CPU 时间，不能只看日志。
- 每个阶段都保存模型，方便续跑和读回。
