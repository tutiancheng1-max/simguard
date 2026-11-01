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

在 `manifest.json` 里这样写：

```json
"readback": {
  "comsol_tensor_models": ["models/prepared.mph"],
  "anisotropic_ok": [{"physics": "tte", "feature": "cfeq1", "reason": "这里确实是各向异性"}]
}
```

- 默认检查标签为 `tte`、`ttl`、`tts` 的物理场，以及场变量为 `T`、`Te`、`Tl` 的系数型 PDE。其他模型要在 `simguard.project.json` 的 `comsol_tensor` 里配置。
- 没找到目标张量时，结果是 FAIL。这通常说明标签配错了，不能当作没有问题。
- 目前只自动检查系数型 PDE 的 `c`。COMSOL 内置的传热接口用 `k`，需要自己导出读回并用 `dump` 和 `checks` 核对。
- 还要核对运行时真正使用的模型，不只是建模阶段保存的副本。
- 轴对称模型要确认 `r` 权重写进了 `c`、`a`、`f`、`da` 和界面通量。

## 运行

- 用 `comsolbatch -np $env:SIMQ_CORES`，通过 `simq` 提交。
- 内存可能随线程数增长。先用小模型测两个线程数；内存不够时试着减少线程，不要一味加线程。
- 大模型的直接求解器分解时可能很久不写日志。判断是否停滞要看 CPU 时间，不能只看日志。
- 每个阶段都保存模型，方便续跑和读回。
