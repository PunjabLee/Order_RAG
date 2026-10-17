# AGENTS.md — AI 编码代理项目上下文入口

> 本文件是编码代理（Qoder subagent / 其他 AI 工具）进入本仓库的第一读址。规则不新造：全部条目可在权威文档中溯源，冲突时以权威文档为准。

## 1. 项目定位

**基于 RAG 的电商智能对话下单系统**：对话式购物助手承载咨询/推荐/下单，RAG 侧只做读与"读+草稿"，交易写边界见 ADR-001。

当前阶段：设计基线 **V2.2（研发评审已通过）**；迭代1 已按 G-A 签署（mock/B 基线），迭代2 进行中。仓库现状 = 基线文档 + `contracts/` 契约草案 + `eval/` 评测集 v0 + `scripts/ci/` 工程骨架 + **`src/nlu/` 查询理解业务代码（IT-2.02，待合入态，分支 feature/IT-2.02-nlu-query-understanding）**。

## 2. 权威文档地图（裁决顺序：设计文档 > 迭代计划 > 启动会材料）

| 文件 | 角色 | 引用方式 |
|------|------|---------|
| `设计文档.md` | 唯一需求/设计基线（PRD-x.y / DD-x.y 编号；附录 E 整改表、F Open Questions Q1~Q12、G 修订历史） | 章节号直引，如 `DD-3.6.2` |
| `研发迭代计划.md` | 任务卡 `IT-<迭代>.<序号>`、门禁 G-A~G-D、Git 工作流 §8、执行约定 §7、待裁决台账 §9 | 任务 ID 委派，如 `IT-2.05` |
| `迭代1启动会材料.md` | DD-15 接口人指派、评测集批次派发、G-A 检查单 | 仅迭代1 有效 |
| `docs/adr/README.md` | ADR 模板与待归档清单（ADR-001~003 待签署，IT-1.08） | 新决策自 ADR-004 |

## 3. 常用命令

```bash
sh scripts/ci/install-hooks.sh                      # 安装钩子（commit-msg 校验 + post-commit 自动推送）
python3 scripts/ci/check_mermaid.py                 # mermaid 围栏配对/语法头检查（附录 D）
python3 scripts/ci/recalc_amount.py                 # DD-8.1 金额独立复算（应得 8049700 分）
python3 eval/metrics/validate_schema.py             # 评测集 200 条结构校验
python3 -m unittest discover -s scripts/ci/tests    # CI 脚本单测（26 项）
python3 -m unittest discover -s src/nlu/tests -t .   # 查询理解单测（170 项：归一化/决策表/降级链/零API/守卫）
python3 eval/metrics/intent_accuracy.py --layer rule  # 意图准确率夹具评测（非阻断；当前实测 0.8350<0.92，欠账见计划 §9.2 L-06，禁改门槛蒙混）
```

要求 Python ≥ 3.10（仅标准库）。临时关闭自动推送：`NO_AUTO_PUSH=1 git commit ...`。

## 4. 提交与分支规范（详见计划 §8）

- 分支模型：`main`（保护，仅 release/hotfix 合入打 tag）→ `develop`（迭代集成）→ `feature/IT-x.xx-<简述>`（一任务一分支）；契约走 `contracts/*` 双人 Approve 专道。
- 提交格式：`<type>(<scope>): <描述>`，正文必携行首 `Task: IT-x.xx`（行首多行 `Task:` 拒绝；基线遗留提交用 `Task: N/A: <理由>` + `Baseline: <tag|hash>` 双尾注白名单）与 `Refs: <DD/PRD 章节或 US 用例>`。
- 合入 squash，主线保持"1 任务 ≈ 1 提交"；代码/文档/格式化分提交。
- **迭代交付自动评审**（计划 §7）：迭代编码全部提交后、合入 develop 前、门禁评审前，各须派发 CodeReview 子代理评审（完整性/正确性/影响面），产出含问题编号/等级/定位/修正/验收；缺陷 fix/docs 分提交回写 §9 台账并在 MR 关联提交号。
- **禁止**：直推 main、`--no-verify` 跳钩子、提交凭据或未脱敏 PII。

## 5. Agent 编制（`.qoder/agents/`，随仓共享）

| Agent | 职责 | 权限特征 | 委派入口 |
|-------|------|---------|---------|
| `feature-developer` | 任务卡编码执行（分支→实现→测试→提交→回写） | 读写+Bash | "执行 IT-x.xx" |
| `contract-guardian` | `contracts/` DD-7 契约生成/冻结/兼容性判定 | 读写+Bash | "更新/冻结契约" |
| `eval-annotator` | `eval/` 评测集、夹具、结构校验 | 读写+Bash | "生产/回填评测批次" |
| `gate-reviewer` | G-A~G-D 门禁证据核验 | **只读**+Bash，与产出方隔离 | "评审 G-x" |
| `docs-reconciler` | 状态回写（计划 §1/§6/§9、设计文档 DD-15/附录 E/F/G）与追溯清账 | 仅文档，无 Bash | "回写 IT-x.xx 状态" |

## 6. 硬性约束清单

1. **假设值禁硬编码**（DD-16 R-07）：单价/折扣顺序/TTL 等一律配置化并注释关联 Q 编号（Q1~Q14）；联调结论回来后经 IT-1.07 替换。
2. **金额=整数分**：禁止 float；展示值与 `*_cents` 必须可按 DD-8.1 复算。
3. **五条契约红线**：下单请求体仅 `{draft_id, confirm_token}`；对话请求体无 `context` 对象；金额整数分；事实响应含 `citations` + `fact_check`；错误响应含 `error_code` + `trace_id`。
4. **PII 脱敏**：样本与日志按 DD-5.5 口径（`138****0000`、地址留区县级）。
5. **降级禁交易**（DD-6.3 铁律）：降级态创建订单请求一律 ORD-40903 拒绝。
6. **依赖未闭环不阻塞读侧**：Q1~Q5 结论前禁止交易链路合码，读侧开发可先行（计划 §0 并行说明）。
7. **评审门禁（计划 §7 自动评审机制）**：评审未闭环不得拉 `release/*`；未修复的 High 及以上缺陷不得降级为待办后合入；禁 `--no-verify`。

## 7. 目录结构

```
contracts/          # OpenAPI 契约（chat/orders/sessions/feedback/escalate/notifications + errors-dict + shared-schemas + redocly.yaml）
src/nlu/            # IT-2.02 查询理解：intents(枚举单源)/config+config_data(阈值词表单源)/router(分层管线)/normalize/schema/providers + tests(170)
eval/               # ANNOTATION_GUIDE.md / golden-set-v1(5批次200条) / fixtures / metrics(校验脚本)
scripts/ci/         # commit-msg 校验器 / mermaid 检查 / 金额复算 / 单测 / pricing_baseline.json(假设值配置)
docs/adr/           # ADR 模板与待归档清单
.qoder/agents/      # 五角色 subagent 定义
.githooks/          # commit-msg（规范卡口）+ post-commit（自动推送）
.github/workflows/  # CI 七步流水线（Gate 4 含双 discover：scripts/ci/tests + src/nlu/tests；IT-1.09/IT-2.05/IT-3.09 仍为声明槽位）
```
