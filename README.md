# 跨机构数据协作台账

本服务记录数据来源、字段含义、合作许可、数据产品及交付凭据。合作方可在本地演练不同机构和法域之间的资料流转；示例记录均为虚构，不连接真实口岸或支付系统。

需要 Python 3.10 或更新版本，不依赖第三方包。

运行项目测试：`python -m unittest discover -s tests -v`。
检查源码：`python -m compileall -q exchange`。
演练入口：`python -m exchange --database demo.db`。

领域逻辑分布在来源目录、合作许可、产品编排、交付核验和分级纠正中。`exchange/service.py` 组合这些操作，`exchange/db.py` 保存本地 SQLite 表结构；调用接口和现有边界可从源码及测试读取。

## 分级纠正

来源分级登记有误时，通过 `service.corrections` 走完整的提出、审阅、生效流程：

```python
correction = service.corrections.propose(
    "fix-1", "flow", "restricted", ["plate"], "车牌含个人信息，登记时漏标", "2026-11-06", "登记员甲"
)
impact = service.corrections.impact("fix-1")     # 输入、产品、接收方、字段与规则
service.corrections.review("fix-1", "审阅员乙", "approve", "确认仅车牌敏感", "2026-11-07")
```

- 状态依次为 `proposed` → `approved` → `effective`，审阅也可以给出 `rejected`。
- `review(..., effective=False)` 只通过、不生效；之后再调用 `apply` 明确生效。只有生效的纠正会改变后续交付核验。
- 纠正范围有两种：给出 `sensitive_fields` 时只影响含有这些字段的记录（并按纠正后的分级核验）；不给出字段时按整个来源处理。字段范围纠正不会冻结同来源的其他记录，也不会影响其他来源的产品。
- 审阅时可以用 `sensitive_fields=` 收窄或替换纠正范围；例如按整来源提出的纠正，经审阅确认只有某个字段敏感后，只把该字段标为敏感，来源分级保持不变。
- 生效后，`impact` 与生效时保存的 `impact_snapshot` 列出受影响的 `inputs`、`products`、`deliveries`、`recipients`、`grants`、`fields` 和 `rules`。每个交付项带有对应的 `rule`（法域、分级、目的、决策、规则修订）与 `affected_fields`，可以直接从接收方追到字段与规则。
- 交付请求即使倒填生效前的日期，也按已生效的纠正核验，不能借此绕过新分级。
- 已发出的交付凭据不会被改写，也不会被撤销：`service.delivery.inspect(delivery_id)`（别名 `receipt`）仍返回凭据创建时的 `original_determination`、`input_digest` 和 `policy_revisions`，并在 `corrections` 中追加纠正说明（含纠正编号、事由、审阅说明和生效时间）。
- 纠正的提出、审阅、生效都写入审计日志，可用 `service.history(correction_id)` 查看。
