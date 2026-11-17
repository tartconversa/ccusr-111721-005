import hashlib
import json

from .audit import write
from .corrections import Corrections
from .grants import Grants
from .policy import Policies
from .products import Products


class Delivery:
    def __init__(self, connection):
        self.connection = connection
        self.grants = Grants(connection)
        self.policies = Policies(connection)
        self.products = Products(connection)
        self.corrections = Corrections(connection)

    def send(self, delivery_id, product_id, revision, recipient, purpose, at):
        existing = self.connection.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        if existing:
            if (existing["product_id"], existing["product_revision"], existing["recipient"], existing["purpose"]) != (product_id, revision, recipient, purpose):
                raise ValueError("交付编号已用于其他请求")
            return dict(existing)
        product = self.connection.execute("SELECT state FROM products WHERE id=? AND revision=?", (product_id, revision)).fetchone()
        if product is None or product["state"] != "draft":
            raise ValueError("数据产品修订不可交付")
        inputs = self.products.inputs(product_id, revision)
        if not inputs:
            raise ValueError("数据产品缺少输入")
        reasons, revisions = [], {}
        revisions_by_source = {}
        for asset in inputs:
            source_id = asset["source_id"]
            if asset["state"] != "active" or not asset["source_active"]:
                reasons.append({"source": source_id, "asset": asset["id"], "reason": "inactive"})
            if not self.grants.permits(source_id, recipient, purpose, at):
                reasons.append({"source": source_id, "asset": asset["id"], "reason": "grant"})
            # 纠正生效后，按该记录实际适用的分级核验，而不是继续沿用来源
            # 最初登记的分级。字段范围纠正只影响含有该字段的记录。
            corrected = self.corrections.resolve(asset)
            classification = corrected["classification"]
            decision, policy_revision = self.policies.decision(asset["jurisdiction"], classification, purpose)
            revisions_by_source.setdefault(source_id, set()).add((classification, policy_revision))
            if decision != "allow":
                reason = {
                    "source": source_id,
                    "asset": asset["id"],
                    "reason": "policy",
                    "classification": classification,
                    "jurisdiction": asset["jurisdiction"],
                    "purpose": purpose,
                    "rule_revision": policy_revision,
                }
                if corrected["corrections"]:
                    reason["corrections"] = corrected["corrections"]
                    reason["sensitive_fields"] = corrected["sensitive_fields"]
                reasons.append(reason)
        # 同一来源下的记录可能因字段级纠正适用不同分级，凭据里分别记下
        # 各自核验到的规则修订，避免后写入的值覆盖先前的判定。
        for source_id, pairs in revisions_by_source.items():
            if len(pairs) == 1:
                revisions[source_id] = next(iter(pairs))[1]
            else:
                for classification, policy_revision in pairs:
                    revisions[source_id + ":" + classification] = policy_revision
        if reasons:
            with self.connection:
                write(self.connection, at, recipient, "delivery.reject", product_id, {"revision": revision, "reasons": reasons})
            raise PermissionError(reasons)
        digest = hashlib.sha256("|".join(asset["digest"] for asset in inputs).encode()).hexdigest()
        with self.connection:
            self.connection.execute(
                "INSERT INTO deliveries(id,product_id,product_revision,recipient,purpose,created_at,input_digest,policy_revisions) VALUES(?,?,?,?,?,?,?,?)",
                (delivery_id, product_id, revision, recipient, purpose, at, digest, json.dumps(revisions, sort_keys=True)),
            )
            write(self.connection, at, recipient, "delivery.accept", delivery_id, {"product": product_id, "revision": revision, "inputs": [a["id"] for a in inputs]})
        return dict(self.connection.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone())

    def inspect(self, delivery_id):
        """返回历史凭据及其原判定和后续的分级纠正说明。

        凭据创建时记录的接收方、输入摘要和规则修订不会因为纠正而改写；
        纠正说明以追加记录的方式呈现。
        """
        row = self.connection.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        receipt = dict(row)
        receipt["policy_revisions"] = json.loads(receipt["policy_revisions"])
        notes = self.connection.execute(
            "SELECT dc.correction_id,dc.original_determination,dc.note,dc.recorded_at,"
            "c.new_classification,c.previous_classification,c.effective_at "
            "FROM delivery_corrections dc JOIN corrections c ON c.id=dc.correction_id "
            "WHERE dc.delivery_id=? ORDER BY dc.recorded_at,dc.correction_id",
            (delivery_id,),
        ).fetchall()
        receipt["corrections"] = [
            {
                "correction_id": note["correction_id"],
                "note": note["note"],
                "recorded_at": note["recorded_at"],
                "corrected_classification": note["new_classification"],
                "previous_classification": note["previous_classification"],
                "effective_at": note["effective_at"],
                "original_determination": json.loads(note["original_determination"]),
            }
            for note in notes
        ]
        receipt["original_determination"] = {
            "recipient": receipt["recipient"],
            "purpose": receipt["purpose"],
            "created_at": receipt["created_at"],
            "input_digest": receipt["input_digest"],
            "policy_revisions": receipt["policy_revisions"],
        }
        return receipt

    # 语义更直观的别名。
    receipt = inspect
