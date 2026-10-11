# contracts/ — API 契约（IT-1.09 契约冻结预备）

权威唯一来源：《设计文档.md》V2.2 基线 **DD-7 全章**（7.1 总览 / 7.2 通用规范 / 7.3 对话 SSE / 7.4 草稿 / 7.5 订单 / 7.6 幂等 / 7.7 其余接口），并交叉引用 DD-6.1（18 行异常矩阵）、DD-4.1~4.4（状态机/SAGA/幂等/TTL）、DD-3.3.1（槽位 1:1）、DD-3.6.2/3.6.3（citations/fact_check）、DD-8.1/8.2/8.4（金额与时间口径）、DD-9（表结构投影）、DD-5（鉴权/审计/授权矩阵）。

当前状态：**frozen-candidate（冻结预备）**。经契约变更评审（见下"冻结流程"）通过后即进入 IT-1.09 冻结态（只读模式）。

## 一、文件清单与 DD-7 小节对照

| 文件 | 覆盖接口/内容 | 对应 DD 小节 |
|---|---|---|
| `chat-v1.yaml` | `POST /api/v1/chat`（SSE：meta/slots/options/delta/citations/fact_check/done/error 八帧；type 八枚举收口） | DD-7.3（主）、DD-7.1 对话行、DD-7.6、DD-6.3、PRD-4.1 |
| `orders-v1.yaml` | `POST /orders/drafts`、`GET /orders/drafts/{id}`、`POST /orders`、`GET /orders/{id}`、`POST /orders/{id}/cancel` | DD-7.4、DD-7.5（主）、DD-7.1 草稿/订单行、DD-4.1~4.4、DD-7.6、DD-8.1、DD-9.3 |
| `sessions-v1.yaml` | `GET /api/v1/sessions/{id}/slots`（多端同步/重拉） | DD-7.1 会话历史行、DD-3.2.3、DD-3.3.1、DD-9.1 |
| `feedback-v1.yaml` | `POST /api/v1/feedback`（赞踩/纠错/期望答案，message_id 去重） | DD-7.1 反馈行、DD-7.7、DD-11.5、DD-9.8 |
| `escalate-v1.yaml` | `POST /api/v1/chat/escalate`（交接体服务端组装） | DD-7.1 转人工行、DD-7.7、US-009、DD-6.4 |
| `notifications-v1.yaml` | `POST /api/v1/notifications/arrival`（sku+user 去重） | DD-7.1 到货通知行、DD-7.7、US-003-S4、M-3 |
| `errors-dict.yaml` | 错误码字典（DD-7.2 十一例 + DD-6.1 逐行收编，共 23 码；含 http_status/retryable/degraded） | DD-7.2、DD-6.1、DD-6.2/6.3 |
| `shared-schemas.yaml` | 统一响应包、Money（分整数+currency+display）、时间（UTC ISO8601+tz）、Slot（address_id/coupon_id 1:1）、Citation、FactCheck、状态/类型枚举 | DD-7.2、DD-3.3.1、DD-3.6、DD-4.1、DD-8、DD-9.3 |

版本策略：URL 主版本 `/v1`；契约文件版本随 `info.version` 递增（DD-7.1）。

## 二、五条红线自查清单（每次提交前逐项执行）

| # | 红线 | 判定口径 | 自查方法（本仓库当前结果） |
|---|---|---|---|
| R-1 | 下单请求体**不含**任何价格/优惠/数量/确认类客户端字段（C-2） | `PlaceOrderRequest` 仅 `{draft_id, confirm_token}`，`additionalProperties:false`；废弃字段清单见 `x-removed-fields`（sku/quantity/coupon_id/confirmed/total_amount） | ✅ orders-v1.yaml |
| R-2 | 对话请求体**不含** `context` 对象，仅可选 `context_ref`（C-6） | `ChatRequest.additionalProperties:false` + `x-removed-fields.context`；`context_ref` 可空只读指针 | ✅ chat-v1.yaml |
| R-3 | 金额一律整数分，禁止 number/float（H-9/DD-7.2） | 全部 `*_cents` 字段 `type: integer`；`grep -n "cents" contracts/*.yaml` 无一 float/number 声明；示例 8049700 可按 DD-8.1 breakdown 求和复算 | ✅ 全文件 |
| R-4 | 每个事实类响应含 `citations[]` 与 `fact_check.status`（C-5） | chat-v1 `x-sse-protocol.fact_rule/no_evidence_rule/blocked_rule`（DD-3.6.3 三道防线进契约）；OpenAPI 无法机器强制帧序，由契约测试断言（CI 项） | ✅（帧序断言待 M1 mock 用例落码） |
| R-5 | 所有错误响应含 `error_code / trace_id` | 统一 `ErrorObject`（required: error_code/message/trace_id）；SSE 流内 error 帧复用同结构 | ✅ 全文件 |

## 三、兼容性规则（DD-7.1，冻结前与冻结后同等生效）

1. **只增不改不删**：允许新增——可选字段、新枚举值（枚举新增按"增"处理，但 `ChatResponseType` 八枚举与错误码前缀为收口枚举，扩充即破坏，需评审）、新端点、新响应码位。禁止——删除字段、改字段类型/语义/必填性、改枚举既有含义、改状态码映射。
2. **废弃路径**：字段废弃走 `Deprecation` 响应头 + **1 个季度过渡**（DD-7.1 版本策略注）；废弃字段在契约中保留并标 `deprecated: true`，不得直接删除。
3. **破坏性变更**：守门人职责——一律拒绝现场放行，升级为**契约变更评审**：技术 Owner + 架构双人 Approve（研发迭代计划 §8.2 `contracts/*` 专道、§7 变更控制、§8.4 特殊资产管控），且合入前前端/交易两消费者的契约测试必过。
4. **版本号**：任何合入即递增 `info.version`；主版本变更（/v2）需新文件，不得原地改 v1 契约。
5. **示例值治理**（设计文档 〇.1）：所有示例（`SKU_A`、`dft_01H…`、`ORD20261011001`、`promo_batch_9`、¥80,497.00/8049700 分、`pv_20261011_0930` 等）均标注"示例值，以实际服务返回为准"；金额示例可按 DD-8.1 独立复算至分位一致；M2 末前以促销/商品服务联调返回值替换基线。

## 四、冻结流程（IT-1.09）

```
预备（本目录当前态）
  → lint：OpenAPI 3.0 validator（CI 项，IT-1.11 流水线接入）
  → mock：契约 mock server 起服，回放 US-001~US-004 Happy 场景（G-A 门禁）
  → 消费者契约测试：前端（SSE 帧渲染/降级渲染）+ 交易（草稿/下单/幂等重放）两方跑绿
  → 评审：文档冲突清单（下表）逐项裁决 → 技术 Owner + 架构双人 Approve
  → 冻结：打契约基线 tag，进入只读模式（后续新增走 MR 契约专道 + 版本递增 + Deprecation 声明）
```

## 五、文档冲突与缺口（不自行改设计，待冻结评审裁决）

**冲突（errors-dict.yaml `x-document-conflicts` 同步维护）：**

| 编号 | 内容 | 契约处置 |
|---|---|---|
| C-① | DD-7.2 声明的 HTTP 状态码示例集合（401/403/409/422/429/503）小于码号段自然语义所需（404/410/422/502/504/451） | 按号段+场景映射并逐码标注，未擅改任何错误码本身 |
| C-② | `ORD-40903`（降级态禁交易）号段暗示 409，成因却是服务端降级（DD-6.3） | 字典暂取 503（可 Retry-After），待裁决 |
| C-③ | `NLU-502`（DD-6.1#11）、`LOGI-42201`（DD-2 正文）前缀不在 DD-7.2 六前缀枚举内 | 原码收编存档并标注"禁止扩散"，建议评审收口为 LLM-/RAG- 前缀 |
| C-④ | DD-5.2 SESSION_OWNER_MISMATCH（401+审计）与 token 无效无分配错误码（AUTH- 前缀仅 AUTH-40302 一例） | 契约以通用 401 ErrorEnvelope 承载，码位待补 |
| C-⑤ | DD-7.3 帧示例序列未含 `error` 帧（任务卡 IT-1.09 要求八帧） | chat-v1 增补 `x-sse-events.error`（复用 DD-7.2 ErrorObject），不改既有七帧结构 |

**缺口（DD 未定义、契约已按最小面收口，评审确认）：**

- G-①：DD-7.4 未显式给出草稿创建**请求体** → 收口为 `{session_id}`（服务端槽位权威，C-2 同构免疫）。
- G-②：同槽位指纹重复创建草稿是否返回同一 `draft_id`（DD-7.1 幂等列仅注"服务端基于槽位"）→ 待交易服务联调定案。
- G-③：`GET /orders/drafts/{id}` 是否回传 `confirm_token` 未定义 → 契约默认不回传（一次性凭据仅创建时下发）；DD-7.5 订单响应示例缺 `amount_display`（DD-7.2 通用规范要求）→ 以可选字段补充。
- G-④：`pending_clarification` 结构未给出 → 契约置为可空对象透传，版式待 M1。
- G-⑤：反馈/转人工两行错误码前缀为"—" → 仅用通用状态码，反馈域专属码待分配。
- G-⑥：转人工"幂等—"下重复触发的合并语义未定义 → 契约写实现约定（并入既有工单/队列），改判走专道。
- G-⑦：DD-8.1 breakdown 示例中 `bulk_discount_10pct = -900000` 与精确 10%（-899970 分）差 30 分——文档以 DD-8.2 尾差规则收口（三行求和=total_cents 8049700 成立）；契约**原样镜像文档示例**并标"示例值，以实际服务返回为准（Q7）"，未改任何数字。

## 六、lint / mock / 契约测试执行说明

- OpenAPI lint：`npx @redocly/cli lint contracts/<file>.yaml`（跨文件 `$ref` 指向同目录 `shared-schemas.yaml`，需在 contracts/ 下执行；CI 基线归 IT-1.11）。
- 错误码字典为纯数据文件（非 OpenAPI），校验口径：前缀 ∈ {RAG-, ORD-, AUTH-, LLM-, SAFE-, GW-}（NLU-/LOGI- 例外见 C-③）、每码含 `http_status/retryable/degraded`、与 DD-6.1 十八行逐行对照无遗漏。
- mock：以冻结后契约为唯一 mock 源（G-A："契约 mock 可回放 US-001~US-004 Happy 场景"）。
- 消费者契约测试：前端消费者（SSE 帧序与降级渲染）、交易消费者（下单幂等重放/异体 409/废弃字段拒绝）；MR 触发重跑（迭代计划 §8.4）。

## 七、禁止事项（守门人工作约束）

- 不执行 git 写操作（add/commit/branch 由开发者自行决定，本目录变更走 MR 专道）。
- 不修改 `contracts/` 之外的文件；不修改《设计文档.md》——出入一律记入上表，不自行改设计。
