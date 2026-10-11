# 基于RAG的电商智能对话下单系统

企业采购场景的对话式导购与下单系统：**RAG 编排层只做读侧**（咨询/检索/试算展示），
交易写入由应用服务层交易服务唯一执行（ADR-001，DD-1.2）。当前处于迭代1（契约冻结与依赖验证）
开工准备期，仓库以基线文档 + CI 工程骨架为主，业务代码自各任务卡分支起逐步合入。

## 基线文档（唯一权威，三件套）

| 文档 | 角色 | 版本 |
|------|------|------|
| [设计文档.md](./设计文档.md) | 需求/设计唯一基线（PRD-x.y / DD-x.y / ADR-00x / 附录 A~G） | V2.2（研发评审通过） |
| [研发迭代计划.md](./研发迭代计划.md) | 执行层派生：任务卡 `IT-<迭代>.<序号>`、门禁 G-A~G-D、Git 工作流（§8） | SPRINT-V1.0 |
| [迭代1启动会材料.md](./迭代1启动会材料.md) | 迭代1 指派表与 G-A 关键路径接口人 | — |

冲突裁决顺序：**设计文档 > 迭代计划 > 本 README/脚本注释**（计划§7 文档同步条款）。

## 目录结构

```
.
├── 设计文档.md / 研发迭代计划.md / 迭代1启动会材料.md   # 基线三件套
├── .githooks/commit-msg              # 提交信息卡口钩子（§8.3）
├── .github/workflows/ci.yaml         # CI 流水线四步 + 后续任务 TODO 槽位（IT-1.11）
├── scripts/ci/                       # 卡口脚本与金额复算基线配置（见其 README）
│   ├── check_commit_msg.py / check_mermaid.py / recalc_amount.py
│   ├── pricing_baseline.json         # 假设值配置（挂 Q1/Q7，禁硬编码，DD-16 R-07）
│   ├── install-hooks.sh
│   └── tests/                        # 单测框架种子（DD-13 单元层）
├── docs/adr/                         # ADR 模板与待归档清单（IT-1.08/Q10 承接）
├── contracts/                        # OpenAPI 契约目录（IT-1.09，由后端+前端负责）
└── eval/                             # 评测集骨架：golden-set-v1 / fixtures / metrics
    （IT-1.11 标注任务与 IT-2.05 回归由测试+数据中台负责）
```

## 快速开始

```bash
# 1. 安装版本化 Git 钩子（commit-msg 规范卡口 + post-commit 自动推送；仅 git config core.hooksPath .githooks，仓库级）
sh scripts/ci/install-hooks.sh
# 提交后自动推送当前分支到 origin；临时关闭：NO_AUTO_PUSH=1 git commit ...

# 2. 本地跑通 CI 同等卡口（详见 scripts/ci/README.md）
python3 -m unittest discover -s scripts/ci/tests   # 单元自测
python3 scripts/ci/check_mermaid.py                # mermaid 围栏/语法头（附录 D/L-3）
python3 scripts/ci/recalc_amount.py                # DD-8.1 金额复算=8049700分（¥80,497.00）

# 3. 开发流程：从 develop 拉任务分支（分支名必含任务 ID），提交须过钩子
#    git checkout -b feature/IT-<迭代>.<序号>-<简述> develop
#    提交格式：<type>(<scope>): <描述> + 正文 Task: IT-x.xx + Refs: <DD/PRD/US>
```

要求 Python ≥ 3.10（仅标准库，无第三方依赖）。

## 工程约定速查（详见计划 §8）

- **一任务一分支、一任务一交付**；合入 squash，主线保持 1 任务≈1 提交；
- 每条提交必携 `Task: IT-x.xx`（进度看板与 DoD 回写依赖）；禁 `--no-verify`；
- 契约改动（接口字段/错误码/枚举）只走 `contracts/*` 分支，双人 Approve + 消费者契约测试；
- 假设值（单价/叠加顺序/TTL 等）一律读配置并注 Open Question 编号，Q 关闭前禁止硬编码；
- `develop` 合入门禁：1 Approve + CI 绿（本仓库四个 Gate 见 `.github/workflows/ci.yaml`）。
