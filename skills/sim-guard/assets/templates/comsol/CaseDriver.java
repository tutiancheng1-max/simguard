// COMSOL 驱动模板。build 阶段只建模并保存，solve 阶段读取保存的模型求解。
// 先把这个算例跑通再改物理。两个阶段分开，才能在求解前读回已保存的模型。
//
// 算例：单位方块上的 -div(c grad T) = q sin(pi y/L)，上下边界 T=0，热量沿 y 方向流动。
// 解析解：面平均 T = 2 q L^2 / (pi^3 k)。
import com.comsol.model.Model;
import com.comsol.model.util.ModelUtil;

public class CaseDriver {
  public static void main(String[] args) throws Exception {
    run();
  }

  // COMSOL 默认的安全设置不允许 Java 读环境变量，路径和阶段由 run_case.py 写进 CaseConfig。
  public static Model run() throws Exception {
    String prepared = CaseConfig.RUN_DIR + "/prepared.mph";
    if (CaseConfig.STAGE.equals("build")) {
      Model m = build();
      m.save(prepared);
      return m;
    }
    Model m = ModelUtil.load("case", prepared);
    solve(m);
    return m;
  }

  static Model build() {
    Model m = ModelUtil.create("case");
    m.param().set("k", "2");
    m.param().set("q", "1");
    m.param().set("L", "1[m]");
    m.param().set("hmax", "0.1[m]");
    for (String[] p : CaseConfig.PARAMS) m.param().set(p[0], p[1]);   // 参数扫描的取值，来自 params.json

    m.component().create("comp1", true);
    m.component("comp1").geom().create("geom1", 2);
    m.component("comp1").geom("geom1").create("r1", "Rectangle");
    m.component("comp1").geom("geom1").feature("r1").set("size", new String[]{"L", "L"});
    m.component("comp1").geom("geom1").run();

    m.component("comp1").physics().create("pde", "CoefficientFormPDE", "geom1");
    m.component("comp1").physics("pde").field("dimensionless").field("T");
    m.component("comp1").physics("pde").field("dimensionless").component(new String[]{"T"});
    // 张量要写完整。只设置一个分量时，其余分量保留默认值，模型照样能算。
    m.component("comp1").physics("pde").feature("cfeq1").set("c", new String[]{"k", "0", "0", "k"});
    m.component("comp1").physics("pde").feature("cfeq1").set("f", "q*sin(pi*y/L)");
    m.component("comp1").physics("pde").create("dir1", "DirichletBoundary", 1);
    m.component("comp1").physics("pde").feature("dir1").selection().set(new int[]{2, 3});

    m.component("comp1").mesh().create("mesh1");
    m.component("comp1").mesh("mesh1").create("ftri1", "FreeTri");
    m.component("comp1").mesh("mesh1").feature("size").set("custom", "on");
    m.component("comp1").mesh("mesh1").feature("size").set("hmax", "hmax");
    return m;
  }

  static void solve(Model m) throws Exception {
    m.study().create("std1");
    m.study("std1").create("stat", "Stationary");
    m.sol().create("sol1");
    m.sol("sol1").study("std1");
    m.sol("sol1").createAutoSequence("std1");

    // 网格收敛：同一模型用两种网格求解
    String[] sizes = {"0.1[m]", "0.05[m]"};
    double[] avg = new double[sizes.length];
    for (int i = 0; i < sizes.length; i++) {
      m.param().set("hmax", sizes[i]);
      m.component("comp1").mesh("mesh1").run();
      m.sol("sol1").runAll();
      if (i == 0) {   // 数据集 dset1 由第一次求解自动生成
        m.result().numerical().create("av1", "AvSurface");
        m.result().numerical("av1").set("data", "dset1");
        m.result().numerical("av1").selection().all();
        m.result().numerical("av1").set("expr", new String[]{"T"});
      }
      avg[i] = m.result().numerical("av1").getReal()[0][0];
    }
    m.save(CaseConfig.RUN_DIR + "/solved.mph");
    // 结果打印到日志，由 run_case.py 取出写成 result.json
    System.out.println("SG_RESULT|{\"avg_T_coarse\": " + avg[0] + ", \"avg_T_fine\": " + avg[1] + "}");
  }
}
