import tempfile
import unittest
from pathlib import Path

from exchange import Exchange


class ClassificationCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Exchange(Path(self.temp.name) / "exchange.db")
        self.service.catalog.register(
            "flow",
            "机构甲",
            "甲地",
            "business",
            [("count", "通行计数", "integer", False), ("plate", "车牌", "string", False)],
            "2026-11-01",
        )
        self.service.catalog.register(
            "other", "机构丙", "丙地", "business", [("count", "通行计数", "integer", False)], "2026-11-01"
        )
        self.service.catalog.ingest("item-1", "flow", {"count": 8, "plate": "甲A1"}, "2026-11-02")
        self.service.catalog.ingest("item-2", "flow", {"count": 9}, "2026-11-02")
        self.service.catalog.ingest("item-3", "other", {"count": 4}, "2026-11-02")
        for grant_id, source_id in (("g-1", "flow"), ("g-2", "other")):
            self.service.grants.issue(grant_id, source_id, "机构乙", "运行协调", "2026-12-31", "2026-11-03")
        self.service.policies.set("甲地", "business", "运行协调", "allow", "2026-11-03", "机构甲")
        self.service.policies.set("甲地", "restricted", "运行协调", "deny", "2026-11-03", "机构甲")
        self.service.policies.set("丙地", "business", "运行协调", "allow", "2026-11-03", "机构丙")
        self.mixed = self.service.products.assemble("mixed", "混合汇总", ["item-1", "item-3"], "2026-11-04", "机构甲")
        self.clean = self.service.products.assemble("clean", "来源内计数", ["item-2"], "2026-11-04", "机构甲")
        self.other = self.service.products.assemble("other", "其他来源", ["item-3"], "2026-11-04", "机构甲")
        self.old = self.service.delivery.send("d-old", "mixed", self.mixed, "机构乙", "运行协调", "2026-11-05")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def _propose(self):
        return self.service.corrections.propose(
            "fix-1", "flow", "restricted", ["plate"], "车牌含个人信息，登记时漏标", "2026-11-06", "登记员甲"
        )

    def test_correction_needs_review_before_it_takes_effect(self):
        self._propose()
        self.assertEqual(self.service.corrections.get("fix-1")["status"], "proposed")
        # 未审阅前不改变交付核验。
        self.service.delivery.send("d-before", "mixed", self.mixed, "机构乙", "运行协调", "2026-11-06")

        record = self.service.corrections.review(
            "fix-1", "审阅员乙", "approve", "确认仅车牌字段敏感", "2026-11-07", effective=False
        )
        self.assertEqual(record["status"], "approved")
        self.assertEqual(record["reviewer"], "审阅员乙")
        # 通过但尚未生效时，仍不拦截。
        self.service.delivery.send("d-approved", "mixed", self.mixed, "机构乙", "运行协调", "2026-11-07")

        self.service.corrections.apply("fix-1", "2026-11-08")
        self.assertEqual(self.service.corrections.get("fix-1")["status"], "effective")
        with self.assertRaises(PermissionError):
            self.service.delivery.send("d-after", "mixed", self.mixed, "机构乙", "运行协调", "2026-11-09")

    def test_default_review_applies_immediately(self):
        self._propose()
        record = self.service.corrections.review("fix-1", "审阅员乙", "approve", "确认", "2026-11-07")
        self.assertEqual(record["status"], "effective")
        self.assertEqual(record["effective_at"], "2026-11-07")

    def test_backdated_request_cannot_bypass_effective_correction(self):
        self._propose()
        self.service.corrections.review("fix-1", "审阅员乙", "approve", "确认", "2026-11-07")
        # 交付请求填写纠正生效之前的日期，也仍然按生效后的分级核验。
        self.service.delivery.send("d-presend", "other", self.other, "机构乙", "运行协调", "2026-11-08")
        with self.assertRaises(PermissionError):
            self.service.delivery.send("d-backdated", "mixed", self.mixed, "机构乙", "运行协调", "2026-11-01")

    def test_reject_leaves_delivery_unchanged(self):
        self._propose()
        record = self.service.corrections.review("fix-1", "审阅员乙", "reject", "不属于敏感字段", "2026-11-07")
        self.assertEqual(record["status"], "rejected")
        with self.assertRaises(ValueError):
            self.service.corrections.apply("fix-1", "2026-11-08")
        self.service.delivery.send("d-ok", "mixed", self.mixed, "机构乙", "运行协调", "2026-11-08")

    def test_field_correction_does_not_freeze_unrelated_sources_or_records(self):
        self._propose()
        self.service.corrections.review("fix-1", "审阅员乙", "approve", "确认", "2026-11-07")

        self.assertEqual(
            self.service.delivery.send("d-clean", "clean", self.clean, "机构乙", "运行协调", "2026-11-08")["id"],
            "d-clean",
        )
        self.assertEqual(
            self.service.delivery.send("d-other", "other", self.other, "机构乙", "运行协调", "2026-11-08")["id"],
            "d-other",
        )
        with self.assertRaises(PermissionError) as raised:
            self.service.delivery.send("d-blocked", "mixed", self.mixed, "机构乙", "运行协调", "2026-11-08")
        reason = raised.exception.args[0][0]
        self.assertEqual(reason["source"], "flow")
        self.assertEqual(reason["asset"], "item-1")
        self.assertEqual(reason["reason"], "policy")
        self.assertEqual(reason["classification"], "restricted")
        self.assertEqual(reason["rule_revision"], 1)
        self.assertEqual(reason["sensitive_fields"], ["plate"])
        self.assertEqual(reason["corrections"], ["fix-1"])

    def test_impact_lists_inputs_products_recipients_fields_and_rules(self):
        self._propose()
        impact = self.service.corrections.impact("fix-1")
        self.assertEqual([item["asset_id"] for item in impact["inputs"]], ["item-1"])
        self.assertEqual([(item["product_id"], item["revision"]) for item in impact["products"]], [("mixed", 1)])
        self.assertEqual(impact["products"][0]["affected_fields"], ["plate"])
        self.assertEqual(impact["deliveries"][0]["delivery_id"], "d-old")
        self.assertEqual(impact["recipients"], ["机构乙"])
        self.assertEqual(impact["rules"][0]["classification"], "restricted")
        self.assertEqual(impact["rules"][0]["decision"], "deny")
        fields = {field["name"]: field for field in impact["fields"]}
        self.assertTrue(fields["plate"]["proposed_sensitive"])
        self.assertFalse(fields["plate"]["corrected_sensitive"])
        self.assertFalse(fields["count"]["proposed_sensitive"])

        self.service.corrections.review("fix-1", "审阅员乙", "approve", "确认", "2026-11-07")
        fields = {field["name"]: field for field in self.service.corrections.impact("fix-1")["fields"]}
        self.assertTrue(fields["plate"]["corrected_sensitive"])
        self.assertTrue(fields["plate"]["sensitive"])
        self.assertFalse(fields["count"]["corrected_sensitive"])

    def test_effective_snapshot_is_kept_on_correction(self):
        self._propose()
        self.service.corrections.review("fix-1", "审阅员乙", "approve", "确认", "2026-11-07")
        snapshot = self.service.corrections.get("fix-1")["impact_snapshot"]
        self.assertEqual([item["asset_id"] for item in snapshot["inputs"]], ["item-1"])
        self.assertEqual(snapshot["deliveries"][0]["delivery_id"], "d-old")

    def test_historical_receipt_keeps_original_determination_and_gets_note(self):
        self._propose()
        self.service.corrections.review("fix-1", "审阅员乙", "approve", "确认仅车牌敏感", "2026-11-07")
        receipt = self.service.delivery.inspect("d-old")
        self.assertEqual(receipt["input_digest"], self.old["input_digest"])
        self.assertEqual(receipt["policy_revisions"], {"flow": 1, "other": 1})
        self.assertEqual(receipt["original_determination"]["created_at"], "2026-11-05")
        self.assertEqual(receipt["original_determination"]["input_digest"], self.old["input_digest"])
        self.assertEqual(len(receipt["corrections"]), 1)
        note = receipt["corrections"][0]
        self.assertEqual(note["correction_id"], "fix-1")
        self.assertEqual(note["original_determination"]["input_digest"], self.old["input_digest"])
        self.assertIn("车牌", note["note"])
        self.assertIn("确认仅车牌敏感", note["note"])

    def test_correction_audit_chain(self):
        self._propose()
        self.service.corrections.review("fix-1", "审阅员乙", "approve", "确认", "2026-11-07")
        actions = [entry["action"] for entry in self.service.history("fix-1")]
        self.assertEqual(actions, ["correction.propose", "correction.review", "correction.effective"])

    def test_unknown_field_in_correction_rejected(self):
        with self.assertRaises(ValueError):
            self.service.corrections.propose(
                "fix-x", "flow", "restricted", ["not-a-field"], "笔误", "2026-11-06", "登记员甲"
            )

    def test_duplicate_correction_id_rejected(self):
        self._propose()
        with self.assertRaises(ValueError):
            self._propose()

    def test_second_correction_can_widen_scope_after_approval(self):
        self._propose()
        self.service.corrections.review("fix-1", "审阅员乙", "approve", "确认", "2026-11-07")
        impact = self.service.corrections.impact("fix-1")
        self.assertEqual(impact["correction"]["sensitive_fields"], ["plate"])
        # 审阅时可以缩小或修订字段范围，生效时以审阅后的范围为准。
        self.service.corrections.propose(
            "fix-2", "flow", "restricted", [], "整类来源需要复核", "2026-11-08", "登记员甲"
        )
        self.service.corrections.review("fix-2", "审阅员乙", "approve", "整来源复核", "2026-11-09")
        with self.assertRaises(PermissionError):
            self.service.delivery.send("d-clean-2", "clean", self.clean, "机构乙", "运行协调", "2026-11-10")

    def test_review_can_narrow_a_source_wide_correction_to_one_field(self):
        # 先按整个来源提出纠正；审阅发现只有车牌字段敏感，于是缩小范围。
        self.service.corrections.propose(
            "fix-wide", "flow", "restricted", [], "整类来源疑似敏感", "2026-11-06", "登记员甲"
        )
        record = self.service.corrections.review(
            "fix-wide", "审阅员乙", "approve", "复核后仅车牌敏感，其余维持普通业务数据",
            "2026-11-07", sensitive_fields=["plate"],
        )
        self.assertEqual(record["sensitive_fields"], ["plate"])
        impact = self.service.corrections.impact("fix-wide")
        # 来源本身的分级没有被整体改动，只有含车牌字段的记录按 restricted 处理。
        self.assertEqual(impact["source"]["classification"], "business")
        fields = {field["name"]: field for field in impact["fields"]}
        self.assertTrue(fields["plate"]["sensitive"])
        self.assertFalse(fields["count"]["sensitive"])
        # 同来源不含车牌的记录、以及其他来源的产品都不受影响。
        self.service.delivery.send("d-clean-3", "clean", self.clean, "机构乙", "运行协调", "2026-11-08")
        self.service.delivery.send("d-other-3", "other", self.other, "机构乙", "运行协调", "2026-11-08")
        with self.assertRaises(PermissionError):
            self.service.delivery.send("d-mixed-3", "mixed", self.mixed, "机构乙", "运行协调", "2026-11-08")

    def test_source_wide_correction_blocks_all_records_of_that_source(self):
        self.service.corrections.propose(
            "fix-wide-2", "flow", "restricted", [], "整个来源需提高分级", "2026-11-06", "登记员甲"
        )
        self.service.corrections.review("fix-wide-2", "审阅员乙", "approve", "确认", "2026-11-07")
        self.assertEqual(self.service.corrections.impact("fix-wide-2")["source"]["classification"], "restricted")
        with self.assertRaises(PermissionError):
            self.service.delivery.send("d-clean-wide", "clean", self.clean, "机构乙", "运行协调", "2026-11-08")
        # 其他来源仍按自己的分级核验。
        self.service.delivery.send("d-other-wide", "other", self.other, "机构乙", "运行协调", "2026-11-08")


if __name__ == "__main__":
    unittest.main()
