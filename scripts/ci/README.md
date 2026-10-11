# scripts/ci — CI 工程骨架（IT-1.11）

流水线七步卡口的本地实现与运行方法。权威依据：《研发迭代计划.md》§8.2/§8.3/§8.4、
《设计文档.md》DD-13（测试分层）、附录 D（mermaid 渲染检查 L-3）、DD-8（金额规范）。

## 脚本清单

| 脚本 | 用途 | 本地运行 |
|------|------|---------|
| `check_commit_msg.py` | 提交信息校验：Conventional Commits + `Task: IT-x.xx` trailer（§8.3 硬约束①） | `python3 scripts/ci/check_commit_msg.py <MSG_FILE>`；CI 范围模式 `--range origin/develop..HEAD` |
| `.githooks/commit-msg`（仓库根） | commit-msg 钩子包装，调用上述校验器 | 由钩子自动触发，勿手工调用 |
| `install-hooks.sh` | 安装钩子：`git config core.hooksPath .githooks`（仅仓库级 config；随装随生效 commit-msg + post-commit 两钩） | `sh scripts/ci/install-hooks.sh` |
| `.githooks/post-commit`（钩子目录） | 提交成功后自动推送当前分支到 `origin`（首次 `-u` 建跟踪）；护栏：`NO_AUTO_PUSH=1` 开关、rebase/merge/cherry-pick 进行中跳过、游离 HEAD 跳过、无 remote 跳过；失败仅告警不阻断提交，绝不 force | 装钩即生效；验证 `git log origin/<分支>..HEAD` 应为空 |
| `check_mermaid.py` | 扫描 `*.md`：``` 围栏配对（奇数报错）、mermaid 语法头识别（flowchart/graph/stateDiagram/sequenceDiagram）、块内字面 `\n`（附录 D L-3 要求 `<br/>`）；输出文件+围栏号+行号 | `python3 scripts/ci/check_mermaid.py`（默认全仓，可传文件/目录参数） |
| `recalc_amount.py` | 按 DD-8.1 独立复算示例金额（纯整数分）并 grep 设计文档 `total_cents`/展示值/减法链比对，不一致即报 | `python3 scripts/ci/recalc_amount.py`；`--no-doc` 只复算 |
| `pricing_baseline.json` | 复算基线参数（单价分/数量/折扣bp/券分/叠加顺序/舍入步长）。**全部为假设值，挂 Q1/Q7，禁止业务代码硬编码（DD-16 R-07）**；IT-1.07 联调后替换 | 被 `recalc_amount.py` 默认读取 |
| `tests/test_ci_checks.py` | 上述三个校验器的单元自测（DD-13"单元层"骨架种子） | `python3 -m unittest discover -s scripts/ci/tests` |

## commit-msg 规则与豁免白名单

必过条件：`<type>(<scope>): <描述>`（type ∈ feat/fix/docs/refactor/test/ci/chore/perf/build，§8.3）
＋ 正文含行首 `Task: IT-<数字>.<数字>`（允许尾注 `[Qn]`；行首多于一行 `Task:` 即拒绝，正文/标题提及
字面 "Task:" 不计，§8.3 硬约束④升级）＋ 标题与正文之间空一行。返回码非 0 即拒绝。

豁免白名单（代码内注释同步列出理由）：

1. **`Merge` / `Revert` 前缀**：平台/`git revert` 机器生成，非人工撰写；合入 squash（§8.3④）
   后主线不保留 Merge 提交。
2. **`fixup!` / `squash!` 前缀**：交互压缩中间态提交，压缩后的最终提交信息仍受本规则约束。
3. **`Task: N/A: <理由>` + `Baseline: <tag|hash>`**（白名单收敛，评审 M-4/复核 N5）：仅限基线序列
   遗留提交（v2.2-baseline 文档先于任务卡体系产生）；须同时写明理由与显式 `Baseline:` 尾注，缺一拒绝。
   CI 范围模式另认历史豁免登记表 `LEGACY_EXEMPT_COMMITS`（90d894c/942367c/28d0001，收敛规则前提交追认）。

宽松项（告警不阻断）：scope 不在 §8.3 业务枚举内（计划暂未定义 infra 类 scope，见台账 L-03）；
缺 `Refs:`；标题超长。（多条 `Task:` 已由告警升级为拒绝，不再列入本节）

## 快速自检（七连跑；Gate 6 需 node/npm，Gate 7 需本地 docker daemon）

```bash
sh scripts/ci/install-hooks.sh                          # 装钩子（git config + 两钩 chmod）
python3 scripts/ci/check_commit_msg.py --range origin/develop..HEAD  # Gate 1 提交规范
python3 scripts/ci/check_mermaid.py                     # Gate 2 mermaid 检查
python3 scripts/ci/recalc_amount.py                     # Gate 3 金额复算 + 文档比对
python3 -m unittest discover -s scripts/ci/tests        # Gate 4 单元自测
python3 eval/metrics/validate_schema.py                 # Gate 5 评测集结构回归
npx --yes @redocly/cli lint --config contracts/redocly.yaml contracts/{chat,orders,sessions,feedback,escalate,notifications,shared-schemas}-v1.yaml 2>/dev/null || npx --yes @redocly/cli lint --config contracts/redocly.yaml $(ls contracts/*.yaml | grep -vE 'redocly|errors-dict')   # Gate 6 契约 lint
docker run --rm -v "$PWD:/repo" ghcr.io/gitleaks/gitleaks:v8.21.2 detect --source=/repo --redact --no-banner   # Gate 7 DLP（需 docker）
```

## 与流水线的关系

`.github/workflows/ci.yaml`（GitHub Actions 语法）按序执行七步：commit-msg 范围校验 →
mermaid 检查 → 金额复算 → 单元自测 → 评测集结构回归（validate_schema.py）→
契约 lint（redocly，Gate 6，评审 H-1 补位）→ DLP 凭据扫描（gitleaks，Gate 7 最小实现）；
评测语义回归（IT-2.05）、双消费者契约测试（IT-1.09 冻结）、PII 自研规则（IT-3.09）仍为
声明的 TODO 槽位。**GitLab CI / Jenkins 用户**：将上述七步映射为 script 阶段即可，卡口语义
（返回码非 0 阻断）不变；§8.2 `develop` 保护要求"CI 绿"即指本流水线。

## 边界（本目录刻意不做的事）

- `recalc_amount.py` 属"命中模式的字面量一致性"校验：若文档删除全部金额锚点导致 0 命中，仍判通过（无法抓"基线缺失"）；锚点存在性强断言随 IT-1.07 夹具定稿增强（评审 L-2）。

- mermaid 检查是**围栏/语法头/换行标记**级前置校验，非 headless 渲染（渲染级检查与
  `mermaid-cli` 引入在 ci.yaml `TODO: 完整渲染` 槽位声明，待工具链预算确认）；
- 金额复算只**验证**文档基线自洽，不引入任何业务计算依赖——金额权威方仍是促销服务
  （DD-8.2），本工具属"示例数据治理（〇.1/L-2）"的文档侧复算义务；
- 不触碰 `contracts/`、`eval/`（其他任务卡负责）。
