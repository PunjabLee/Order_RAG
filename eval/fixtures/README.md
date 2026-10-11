# 评测夹具清单（fixtures）— IT-1.11 交付物 D3（假设版，待 Q7 回填）

| 项 | 内容 |
|----|------|
| 用途 | 为 `eval/golden-set-v1/*.jsonl` 提供**唯一一致的期望值来源**；同时作为 IT-1.07（数据一致性管线）联调时的最小夹具 |
| 状态 | **全部为假设值**：每个夹具文件顶层带 `"pending_question": "Q7"`、`"is_assumption": true`；IT-2.05（Recall@5 首跑）前必须用真实商品/政策数据回填替换 |
| 权威依据 | 《设计文档.md》DD-7.2/7.4（契约与 breakdown）、DD-8.1~8.4（金额与时间口径）、DD-5.4（授权矩阵）、DD-5.5（PII）、DD-3.4.6（评估基线）；《迭代1启动会材料.md》§3 批次表 + §5 G-A 检查单 |
| 禁止 | 写入真实凭据、真实税号、真实手机号、未脱敏地址；把假设值硬编码进业务代码（G-A 检查项） |

**文件清单**

| 文件 | 内容 | 状态 |
|------|------|------|
| `promotion.json` | 商品 SKU 表 + 促销/政策表 + 叠加互斥矩阵 + **8049700 分推导链** + 时间口径 + 用户/认证/地址/限额 | 已交付（示例，含全部字段级定义） |
| `product.json` / `certification.json` / `address.json` | 建议从 `promotion.json` 的 `skus` / `users` / `addresses` 段拆出，供 IT-1.07 灌库使用 | 本批合并进 `promotion.json`，拆分留待 IT-1.07（避免双源不一致） |

---

## 1. 商品夹具（`skus[]`，字段级）

字段：`sku_id` / `name` / `model_alias[]` / `category` / `unit_price_cents:int` / `currency` / `on_sale:bool` / `warehouse` / `stock_quantity:int` / `preorder_lead_days:int|null` / `bulk_eligible:bool` / `edu_only:bool` / `pricing_version` / `pending_question`

| sku_id | 展示名 | 别名（归一化命中） | 单价(分) | 单价(¥) | 可售 | 主仓 | 库存 | 预售 | 适用批量9折 | 备注 |
|--------|--------|-------------------|---------:|--------:|:---:|------|---:|:---:|:---:|------|
| `SKU_A` | Mac Studio M2 Ultra 64G/1T | Ultra/64G/1T/M2 Ultra | 2,999,900 | 29,999.00 | ✓ | SH_MAIN | 12 | — | ✓ | **DD-8.1 锚点基准价** |
| `SKU_B` | Mac Studio M2 Ultra 192G/2T | 顶配/192G/2T | 3,499,900 | 34,999.00 | ✓ | SH_MAIN | 2 | — | ✓ | 库存紧张（B3-028 用） |
| `SKU_C` | Mac Studio M2 Max 32G/1T | 32G/1T/M2 Max/入门 | 1,999,900 | 19,999.00 | ✓ | SH_MAIN | 30 | — | ✓ | 与 DD-7.3 示例价一致 |
| `SKU_D` | Mac Studio M2 Ultra 64G/2T | 64G/2T/2T | 3,299,900 | 32,999.00 | ✓ | CD_MAIN | 5 | — | ✓ | 成都仓 +2 工作日；CDC 延迟样本用 |
| `SKU_E` | Mac Pro | 顶配/Mac Pro | 5,599,900 | 55,999.00 | ✓ | SH_PREORDER | 0 | 30 | ✓ | 预售（B3-005/B3-029） |
| `SKU_F` | Studio Display | 显示器/Screen | 1,299,900 | 12,999.00 | ✓ | SH_MAIN | 40 | — | ✗ | 配件类不适用批量9折（B2-033/B4-004） |
| `SKU_G` | Mac Studio M1 Ultra 48G/1T | 老款/M1 | 2,499,900 | 24,999.00 | ✗ | — | 0 | — | ✗ | 已下架（B1-039~ RAG-40401） |
| `SKU_H` | Mac Studio 教育版 64G/1T | 教育版/学生价 | 2,999,900 | 29,999.00 | ✓ | SH_MAIN | 8 | — | ✗ | 仅学生身份（EDU 互斥 BULK） |

**冲突裁决记录（必须留档）**：DD-7.3 SSE 示例中出现的 `1999900 / 3499900` 为**示意值**；本夹具以 DD-8.1 + DD-7.4 `pricing_breakdown` 为权威，故 `SKU_A.unit_price_cents = 2999900`（3×2999900=8999700 才能复算出锚点 8049700），`1999900` 归给 `SKU_C`。该裁决不修改设计文档本身，仅在夹具侧标注，待 **Q7** 回填时复核。

## 2. 促销/政策夹具（`policies[]`）

字段：`policy_id` / `name` / `kind(discount|threshold|coupon|freight|warranty|limit|delivery|cert)` / `params{}` / `applies_to_identity[]` / `stackable_with[]` / `exclusive_with[]` / `effective_from` / `effective_to` / `status` / `policy_version` / `doc_id` / `pending_question`

| policy_id | 关键参数 | 现行/过期 | effective_to | policy_version | doc_id |
|-----------|---------|:--------:|--------------|----------------|--------|
| `BULK_ENTERPRISE` | `min_quantity=3`（同一 SKU）、`rate=0.9` | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_bulk_enterprise` |
| `THRESHOLD_500` | `threshold_cents=8000000` → `amount_cents=-50000` | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_threshold_500` |
| `EDU_DISCOUNT` | `rate=0.95`，`identity=student_only` | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_edu` |
| `NEWCUST_300` (`cpn_newcust_300`) | `amount_cents=-30000` | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_newcust` |
| `STACK_MATRIX` | 见 §3 | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_stack_matrix` |
| `FREE_FREIGHT` | `freight_cents=0`（≥1台，中国大陆可达区域） | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_free_freight` |
| `PREORDER_30D` | `lead_days=30`（自然日） | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_preorder_30d` |
| `WARRANTY_STANDARD` | `months=12` | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_warranty` |
| `DELIVERY_STANDARD` | `earliest=+3wd, latest=+5wd, cutoff=15:00` | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_delivery_std` |
| `DELIVERY_COVERAGE` | 偏远 `+3wd`；不可达表含 `addr_003` | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_coverage` |
| `WAREHOUSE_CD_OFFSET` | `+2wd` | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_warehouse_cd` |
| `PURCHASE_LIMIT` | `quantity 1~20`；`single_order_cap_cents=50000000` | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_purchase_limit` |
| `DRAFT_TTL` | `ttl_seconds=900`，`price_lock=false` | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_draft_ttl` |
| `INVOICE_STANDARD` | `tax_included=true`，`price_adjustment_cents=0` | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_invoice` |
| `ORDER_CONFIRM` | 必须 `draft_id + confirm_token` | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_order_confirm` |
| `ENTERPRISE_CERT` | 企业认证三点校验（试算/草稿/下单） | 现行 | 2026-12-31 | `pv_20261011_0930` | `doc_pol_cert_enterprise` |
| `DOUBLE11_2025` | 过期（陷阱） | 过期 | 2025-11-11 | `pv_2025_1020` | `doc_pol_double11_2025` |
| `THRESHOLD_300_2025` | 过期（陷阱）−3000 | 过期 | 2025-12-31 → 实际 2026-03-31 | `pv_2025_300` | `doc_pol_threshold_300_2025` |
| `THRESHOLD_800_2025` | 过期（陷阱）−80000 | 过期 | 2026-02-28 | `pv_2025_800` | `doc_pol_threshold_800_2025` |
| `BULK_V1_2025` | 过期（陷阱）门槛 2 台 | 过期 | 2025-12-31 | `pv_2025_bulk1` | `doc_pol_bulk_v1_2025` |
| `CPN_BATCH_8` (`cpn_batch_8`) | 过期（陷阱）8折券 | 过期 | 2026-06-30 | `pv_2026_cpn08` | `doc_cpn_batch_8_2026` |
| `EDU_2025` | 过期（陷阱） | 过期 | 2025-12-31 | `pv_2025_edu` | `doc_pol_edu_2025` |
| `REBATE_2025` | 过期（陷阱）返点 | 过期 | 2025-12-31 | `pv_2025_0301` | `doc_pol_rebate_2025` |
| `WARRANTY_GIFT_2026` | 过期（陷阱）赠延保 | 过期 | 2026-08-31 | `pv_2026_0401` | `doc_pol_warranty_gift_2026` |

## 3. 叠加/互斥矩阵与两条**显式假设**

| | BULK_ENTERPRISE | THRESHOLD_500 | EDU_DISCOUNT | NEWCUST_300 |
|---|:--:|:--:|:--:|:--:|
| BULK_ENTERPRISE | — | **可叠加** | 互斥 | 互斥 |
| EDU_DISCOUNT | 互斥 | 可叠加 | — | 互斥 |
| NEWCUST_300 | 互斥 | 可叠加 | 互斥 | — |

- **假设 A1（叠加顺序）**：先折后减（`list → bulk/edu → threshold → coupon`），标 `pending_question:"Q7"`。若改为先减后折，锚点变体值见 `promotion.json > derivation.alternatives.before_discount_total_cents = 8054730`（B2-012 记录 `alt_sequence_total_cents`）。
- **假设 A2（跨 SKU 组合）**：`BULK_ENTERPRISE` 仅适用于**单一 SKU 的草稿行**；多 SKU 混合草稿不叠加批量折扣，仅按合计判定满减（例：3×A + 3×B = 19,499,400 → −50,000 = **19,449,400**，见 B4-018）。同样标 `Q7`。
- 金额一律整数分；`breakdown[]` 之和必须等于 `total_cents`（0 容差）。

## 4. 时间口径（`time_rules`，B3/B4 复算依据）

- 基准时刻 `now = 2026-10-11T10:05:00Z`，展示时区 `Asia/Shanghai`；**评测约定**：`now` 的时分秒按展示时区挂钟读取（即北京时间 10:05，未过 15:00 截单），避免"UTC 直读"歧义。回填 Q3 时若改为真 UTC 存储需整体平移，脚本会按此约定复算。
- 截单 `15:00`（含当日内，`cutoff_boundary_rule="inclusive"`）；周六日不计入送达工作日（`working_days_only=true`，不考虑法定节假日调休 —— 假设，标 `Q3`）。
- 推进规则：`D = now_date`（未过截单）或 `now_date 之后的第一个工作日`（已过截单）；`earliest = D 之后第 3 个工作日`、`latest = D 之后第 5 个工作日`（均不含 D）。
- 加时：成都仓 `CD_MAIN +2wd`；偏远（`addr_004` 西藏林芝）`+3wd`；不可达（`addr_003` 新疆塔县）→ `LOGI-42201` 拒绝并**禁止生成日期端点**。
- 已复算端点：`10-11 10:05 → 10-14 / 10-16`；`10-11 15:05 → 10-15 / 10-19`；成都仓 `→ 10-16 / 10-20`；林芝 `→ 10-19 / 10-21`；`SKU_E` 预售 30 自然日 `→ 2026-11-10 / 2026-11-20`（脚本对 `preorder` 样本跳过工作日复算）。

## 5. 认证 / 地址 / 限额夹具（字段级）

`users[]`：`user_id` / `tenant_id` / `identity` / `auth_status` / `cert_expiry|null` / `phone_masked`（固定 `138****0000`，禁止明文）

| user_id | tenant_id | identity | 说明 |
|---------|-----------|----------|------|
| `u_1001` / `u_1002` | `t_001` | `individual_verified` | 个人认证有效（不享企业批量折扣） |
| `u_2001` | `t_002` | `enterprise_verified` | 企业认证有效，**锚点场景身份** |
| `u_2002` | `t_002` | `enterprise_pending` | 审核中（语义待 Q6） |
| `u_2003` | `t_002` | `enterprise_expired` | 已于 2026-09-30 过期 |
| `u_guest` | `null` | `guest` | 匿名，仅公开价 |

`addresses[]`：`address_id` / `owner_user_id` / `region_hint`（区县级，DD-5.5）/ `deliverable` / `remote` / `extra_working_days`

| address_id | owner | region_hint | 可达 | 属性 |
|-----------|-------|------------|:---:|------|
| `addr_001` | u_1001 | 上海市浦东新区*** | ✓ | 默认 |
| `addr_002` | u_1001 | 北京市海淀区*** | ✓ | — |
| `addr_003` | u_1001 | 新疆喀什塔县*** | ✗ | `LOGI-42201` |
| `addr_004` | u_1001 | 西藏林芝*** | ✓ | 偏远 `+3wd` |
| `addr_005` | **u_2001** | （不落明文） | — | 非本人 → `AUTH-40302` |
| `addr_006` | u_1001 | 模糊"我公司" | — | 需澄清 |

`limits`：`quantity_min=1`、`quantity_max=20`、`single_order_cap_cents=50000000`、`rate_limit_per_minute=20`、`draft_ttl_seconds=900`、`slot_ttl_seconds=1800`。

## 6. Q7 回填占位约定

1. 每个夹具文件顶层三件套：`"is_assumption": true`、`"pending_question": "Q7"`、`"fill_before": "IT-2.05"`；每条目另有 `effective_to` / `status` 等真实业务字段时**不得**省略 `pending_question` 的块级覆盖。
2. 待回填字段以 `"_fill_hint"` 标注（如单价、折扣率、门槛、截单时点、偏远加时、跨 SKU 口径）；脚本 `validate_schema.py` 在 CLI 传 `--strict-fill` 时对仍为假设的批次报 FAIL，用于 IT-2.05 门禁。
3. 样本侧同步：`b1~b5` 中凡引用本夹具金额的样本必须带 `pending_question`（B1/B2/B4=Q7，B3=Q3，身份态=Q6）；回填后统一替换为 `null` 并把 `review_status` 推进至双签流程。
4. 真实数据到位后：只替换数值、**不得改动样本 id 与期望行为语义**；如需改期望金额，视为标注变更（走双签 + 单独提交，信息标注 `Task: IT-1.11` + 角色）。

## 7. 与 DD-8.1 锚点的对齐（G-A 检查项：夹具金额与复算分位不差）

```
list_price      = 2,999,900 × 3            =  8,999,700 分
bulk_discount   = −round(8,999,700 × 0.1)  =   −900,000 分     ← DD-8.1 声明 9,000.00
after_discount  = 声明 8,099,700 / 严格 8,099,730（差 30 分）
threshold_500   = −50,000                  分
total(锚点)     = 8,049,700 分 = ¥80,497.00   ← 评测必须使用该值（DD-8.1 权威声明）
```
30 分尾差源于"折扣取整口径/叠加顺序"未定，属 **Q7**；`validate_schema.py` 规则：锚点场景强制 `8049700`，其余场景 `breakdown` 求和与 `total_cents` 0 容差，非锚点场景数学偏差 ∈(0,100] 分仅当带 `pending_question:"Q7"` 时记 WARN。
