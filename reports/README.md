# Verification reports

原始产物，由 `tools/` 下的脚本生成。**每个数字都能在这里点开核对。**

宿主绝对路径已清洗为 `<repo>` / `<home>`；其余内容为原始输出（未手工编辑）。
这句话不是靠人记得 —— `python tools/publish_reports.py --check` 是一道**闸门**：
扫到 `X:\Users\<name>` 这类宿主身份路径就 `exit 1`。

| 报告 | 测量什么 | 生成命令 | README 里对应的结论 |
|---|---|---|---|
| [`fault_verify_report.json`](fault_verify_report.json) | 21 场景注入/信号/回滚 | `python tools/fault_injector.py verify` | 故障复现 21/21；注入期信号成立 |
| [`alert_coverage.json`](alert_coverage.json) | 告警覆盖：注入→等够久→声明的告警是否 firing | `python tools/alert_coverage_check.py` | 声明的告警实际触发 20/21 = 95% |
| [`rca_diagnosis_report.json`](rca_diagnosis_report.json) | 端到端：注入→压测→告警→诊断→评分 | `python tools/cluster_rca_verify.py` | 严格 top-1；恢复核对 |
| [`promotion_ab.json`](promotion_ab.json) | 组件级→根因级升级的成对 A/B | `python tools/rootcause_promotion_verify.py` | 严格 17→18/21，top5 20→21/21 |
| [`heldout_report.json`](heldout_report.json) | 未参与调参的输入：措辞 + 同根因不同注入手法 | `python tools/heldout_verify.py --with-faults` | held-out 严格 4/10 = 40% |
| [`crosscluster_report.json`](crosscluster_report.json) | 跨集群归属：根因落在哪个集群（该找谁） | `python tools/crosscluster_verify.py` | 16/18 = 89% |
| [`ontology_v6_report.json`](ontology_v6_report.json) | 第 6 轮本体迭代：成对评估 + 闸门 | `python tools/evolve_ontology_cluster.py --round memory` | ontology_v6 已发布 |
| [`oom_evidence_replay.json`](oom_evidence_replay.json) | 真实告警文本回放：v5 vs v6 的**成对严格命中**（同一批输入，差异只可能来自本体） | `python tools/ontology_ab_replay.py --parent ontology_v5 --candidate ontology_v6` | 第 6 轮的关键词收紧在引擎上**是 19/20 → 19/20（Δ0，零个 case 变化）** |
| [`oom_counterfactual.json`](oom_counterfactual.json) | 反事实：**继续**收紧 `con:oom-detection` 能否救回 `res_cluster_memory` | `python tools/oom_evidence_counterfactual.py` | 严格上限 **19/20**；强推 rc:cluster-capacity 会换来 app_memory_stress + app_oom_kill 回退 |
| [`rca_retest_res_cluster_memory.json`](rca_retest_res_cluster_memory.json) | 单场景端到端复测（v6 激活下真注入 + 真告警 + 真引擎，**修输入之前**） | `python tools/cluster_rca_verify.py res_cluster_memory --no-settle --json-out .chaos/rca_retest_res_cluster_memory.json` | 仍判 `rc:oom-kill` → **严格未命中**（松口径靠候选里的 `metric:mem-pressure` 算命中） |
| [`rca_retest_oom_input_fix.json`](rca_retest_oom_input_fix.json) | 两个内存场景的端到端复测（真注入；`res_cluster_memory` 改声明新的**集群级**告警） | `python tools/cluster_rca_verify.py res_cluster_memory app_memory_stress --no-settle --json-out .chaos/rca_retest_oom_input_fix.json` | `res_cluster_memory → rc:cluster-capacity` ✅、`app_memory_stress → rc:oom-kill` ✅；新告警**没有**为单副本场景触发（`count ≥ 3` 才是集群级信号） |
| [`oom_input_fix_ab.json`](oom_input_fix_ab.json) | 输入 A/B（先导验证）：**同一本体**（v6）、同一 ground truth，只换 `res_cluster_memory` 的输入 | `python tools/ontology_ab_replay.py --parent ontology_v6 --report .chaos/rca_diagnosis_report.json --report-b .chaos/rca_retest_oom_input_fix.json` | 严格 **19/20 → 20/20（+1，0 回退）**，唯一转正 = `res_cluster_memory`（此时尚未做全量重跑，见下一行） |
| [`rca_diagnosis_report_postfix.json`](rca_diagnosis_report_postfix.json) | **全量 21 场景端到端重跑**（输入修复之后；真注入 + 真告警 + 真引擎） | `python tools/cluster_rca_verify.py --json-out .chaos/rca_diagnosis_report_postfix.json` | 注入 21/21、恢复 21/21、告警覆盖 **20/21**；严格 **20/20**（`res_cluster_memory` 判出 `rc:cluster-capacity`） |
| [`e2e_postfix_ab.json`](e2e_postfix_ab.json) | **两轮全量端到端**的成对 A/B（改前基线 vs 改后全量；同一本体、同一 ground truth） | `python tools/ontology_ab_replay.py --parent ontology_v6 --report .chaos/rca_diagnosis_report.json --report-b .chaos/rca_diagnosis_report_postfix.json` | 严格 **19/20 → 20/20**，逐 case 变化 **= 1**（只有 `res_cluster_memory`），**回退 0** |
| [`remediation_exec.json`](remediation_exec.json) | 处置动作执行闭环（含失败自动回滚） | `python tools/remediation_exec.py self-test` | 回滚路径验证 |
| [`host_path_regression.json`](host_path_regression.json) | 宿主绝对路径清理后的**脚本回归**：哪些实跑了、哪些按原因未跑、以及撞出的 4 个既有缺陷 | 由一次性回归脚本产出（按 `tools/_*.py` 约定已删除）；每条的**原始命令与退出码**都在文件内 `results` 字段 | 18 个退出码 0 / 1 个超时 / 8 个注明原因未跑；撞出「一次 `/api/rca/infer` 静默换掉激活本体」等缺陷（已修） |
| [`llm_scripts_regression.json`](llm_scripts_regression.json) | 6 个会真实调 LLM 的脚本实跑（用户授权；真实消耗） | `python tools/...`（6 个脚本逐个跑，见文件内 `results`） | 4/6 退出码 0；自报总量 127,650 tokens；两个非 0 都是既有问题（坏 key 用例不封闭、过期容器名） |
| [`round7_candidate_rejected.json`](round7_candidate_rejected.json) | 第 7 轮候选 patch 的**开轮次前筛**（父 v6 vs 候选，三套固定输入） | `python tools/round_candidate_probe.py` | **REJECT**：21 场景 20/21→19/21、归档严格 19/20→**17/20**；因此**没有开这一轮、没有发布版本** |

## 发布与核对（工具）

| 工具 | 作用 | 命令 |
|---|---|---|
| `tools/publish_reports.py` | 把 `.chaos/` 产物洗净宿主路径后**发布**到 `reports/`；`--check` 是**卫生闸门**（发现 `X:\Users\<name>` 即 exit 1）；`--scrub FILE` 就地清洗指定报告 | `python tools/publish_reports.py` / `--check` / `--scrub reports/x.json` |
| `tools/strict_score_check.py` | 从报告**复算严格命中**（仅可测量场景），并核对"改动只影响了该影响的场景"：转正/回退集合、某条告警的触发范围；断言不成立即 exit 1 | `python tools/strict_score_check.py --baseline reports/rca_diagnosis_report.json --candidate reports/rca_diagnosis_report_postfix.json --expect-improved res_cluster_memory --expect-alert AppClusterMemoryCapacity=res_cluster_memory` |
| `tools/incident_heldout_eval.py` | 把证据收件箱（`.evoontology/rca_evidence_inbox.jsonl`）里的**线上真实事故记录**复算成留存评估集：与"记录时的结论"对比（知识漂移）、能对上已知场景时算**严格命中**、并挑出低置信/无约束支撑的「知识缺口候选」。**只报不改**；基准歧义（同一文本对应互斥期望）会被报出来而不是硬算 | `python tools/incident_heldout_eval.py` |
| `tools/round_candidate_probe.py` | **开轮次前的三道固定输入筛**：候选 patch（内置变体或 `--candidate-records`）对比父版本在 ① 21 场景 ② held-out 考卷 ③ 归档真实告警上的表现；**任一输入集回退即 exit 1** —— 第 7 轮候选就是这样被否掉的 | `python tools/round_candidate_probe.py` |

## 口径说明（重要）

- 这些报告来自**不同实验**，数字不可互相推导：`e2e` 是端到端一轮的 18 个场景，
  `promotion_ab` 是回放真实告警文本的成对 A/B，`fault_verify` 是注入层的能力验证。
- `heldout_report.json` 是本项目**主动公开的负面结果**（措辞换一种说法后严格命中 4/10）。
  修它时**不得**在同一份 held-out 集上调参。
- `tools/incident_heldout_eval.py` 的输入是**线上真实事故记录**，是继 held-out 之后第二份
  「不能拿来调参」的材料：一旦用它去改打分词典/关键词，它就退化成第二份训练集。
  因此该工具**只有报告**这一个动作（不改本体、不改阈值）。
- **严格 ≠ 松口径**：`strict` 只认「最终根因术语 ∈ 期望术语」（`hit_root_cause_exact`），
  而端到端报告自带的 `ok` 还认「期望术语出现在候选/证据/推理里」。`res_cluster_memory`
  曾是这两者的分界（松 20/20、严格 19/20）；**现在已严格命中**——但两个口径仍然不可互换。
- `oom_counterfactual.json` 记录的是**输入撞车**：`app_memory_stress`（单副本）与
  `res_cluster_memory`（3 副本全量）曾拿到**逐字节相同**的 `rca_hint`，
  同一输入只能给同一个 top1 ⇒ 这一对至多命中一个 ⇒ 严格上限被钉在 19/20。
  修法**不是**改 ground truth，而是补掉缺失的**集群级**告警
  （`AppClusterMemoryCapacity`，判据 = 同时越限副本数 ≥ 3）并让该场景声明它。
  单副本场景不会触发它，所以两个场景的输入从此真正可区分；
  **全量重跑已证实**：严格 19/20 → 20/20，逐 case 变化 = 1，回退 0，
  且该告警只在 `res_cluster_memory` 触发（成对分数见 `e2e_postfix_ab.json`，
  真机全量见 `rca_diagnosis_report_postfix.json`）。
- 阈值/窗口等判据的变更都必须走 `tools/evolve_ontology_cluster.py` 的版本化轮次 + 闸门，
  不接受「为了让检查变绿」而直接改数字。
