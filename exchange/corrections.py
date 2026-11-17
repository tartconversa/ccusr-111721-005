import json

from .audit import write


class Corrections:
    """分级纠正的提出、审阅、生效与影响追踪。

    一条纠正先以 ``proposed`` 状态登记，审阅通过后可以立刻生效（默认），
    也可以先停在 ``approved`` 状态，再由 ``apply`` 明确生效。只有生效的
    纠正会改变交付核验；历史凭据本身不会被改写，而是追加纠正说明。
    """

    def __init__(self, connection):
        self.connection = connection

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def propose(self, correction_id, source_id, new_classification, sensitive_fields, note, at, actor):
        if self.connection.execute("SELECT 1 FROM corrections WHERE id=?", (correction_id,)).fetchone():
            raise ValueError("纠正编号已存在")
        source = self._source(source_id)
        fields = self._normalise_fields(source_id, sensitive_fields)
        if new_classification is None:
            new_classification = source["classification"]
        if not new_classification:
            raise ValueError("纠正缺少目标分级")
        if new_classification == source["classification"] and not fields:
            raise ValueError("纠正未改变分类或字段")
        note = note or ""
        with self.connection:
            self.connection.execute(
                "INSERT INTO corrections("
                "id,source_id,status,previous_classification,proposed_classification,proposed_fields,"
                "new_classification,sensitive_fields,previous_sensitive_fields,note,proposed_by,proposed_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    correction_id,
                    source_id,
                    "proposed",
                    source["classification"],
                    new_classification,
                    json.dumps(fields, ensure_ascii=False),
                    new_classification,
                    json.dumps(fields, ensure_ascii=False),
                    json.dumps(self._sensitive_field_names(source_id), ensure_ascii=False),
                    note,
                    actor,
                    at,
                ),
            )
            write(
                self.connection,
                at,
                actor,
                "correction.propose",
                correction_id,
                {
                    "source": source_id,
                    "from": source["classification"],
                    "to": new_classification,
                    "fields": fields,
                    "note": note,
                },
            )
        return self.get(correction_id)

    def review(self, correction_id, reviewer, decision, note, at, new_classification=None, sensitive_fields=None, effective=True):
        """审阅纠正。

        decision 为 ``approve`` 或 ``reject``。通过时可以修订纠正范围；
        ``effective=True``（默认）会在审阅通过的同时生效，``False`` 则只
        记为 ``approved``，等待 ``apply``。
        """
        record = self.get(correction_id)
        if record["status"] not in {"proposed", "approved"}:
            raise ValueError("纠正不在待审状态")
        if decision in {"approve", "approved", "accept"}:
            decision = "approve"
        elif decision in {"reject", "rejected"}:
            decision = "reject"
        else:
            raise ValueError("审阅决定无效")

        if decision == "reject":
            with self.connection:
                self.connection.execute(
                    "UPDATE corrections SET status='rejected',reviewer=?,review_note=?,review_at=? WHERE id=?",
                    (reviewer, note, at, correction_id),
                )
                write(
                    self.connection,
                    at,
                    reviewer,
                    "correction.review",
                    correction_id,
                    {"decision": "reject", "note": note},
                )
            return self.get(correction_id)

        source = self._source(record["source_id"])
        final_classification = new_classification if new_classification is not None else record["new_classification"]
        final_fields = self._normalise_fields(
            record["source_id"],
            sensitive_fields if sensitive_fields is not None else record["sensitive_fields"],
        )
        if final_classification == source["classification"] and not final_fields:
            raise ValueError("纠正未改变分类或字段")
        note = note or ""

        with self.connection:
            self.connection.execute(
                "UPDATE corrections SET status=?,new_classification=?,sensitive_fields=?,reviewer=?,review_note=?,review_at=? WHERE id=?",
                (
                    "approved",
                    final_classification,
                    json.dumps(final_fields, ensure_ascii=False),
                    reviewer,
                    note,
                    at,
                    correction_id,
                ),
            )
            write(
                self.connection,
                at,
                reviewer,
                "correction.review",
                correction_id,
                {
                    "decision": "approve",
                    "from": source["classification"],
                    "to": final_classification,
                    "fields": final_fields,
                    "note": note,
                },
            )
        if effective:
            self.apply(correction_id, at, reviewer)
            return self.get(correction_id)
        return self.get(correction_id)

    def approve(self, correction_id, reviewer, note, at, **scope):
        """审阅通过的快捷入口；其余参数与 ``review`` 相同。"""
        return self.review(correction_id, reviewer, "approve", note, at, **scope)

    def reject(self, correction_id, reviewer, note, at):
        """审阅不通过的快捷入口。"""
        return self.review(correction_id, reviewer, "reject", note, at)

    def apply(self, correction_id, at, actor=None):
        """让已审阅通过的纠正生效。

        生效时只处理该纠正范围内的输入：字段范围纠正只影响含相关字段的
        记录，来源范围纠正才影响该来源的全部记录。其他来源、以及同一来源
        中不含相关字段的记录不会被连带冻结。
        """
        record = self.get(correction_id)
        if record["status"] == "effective":
            return record
        if record["status"] != "approved":
            raise ValueError("纠正尚未通过审阅")
        actor = actor or record["reviewer"] or record["proposed_by"]
        source = self._source(record["source_id"])
        fields = record["sensitive_fields"]
        previous_sensitive = self._sensitive_field_names(record["source_id"])

        with self.connection:
            if fields:
                self.connection.executemany(
                    "UPDATE fields SET sensitive=1 WHERE source_id=? AND name=?",
                    [(record["source_id"], name) for name in fields],
                )
            else:
                self.connection.execute(
                    "UPDATE sources SET classification=? WHERE id=?",
                    (record["new_classification"], record["source_id"]),
                )
            self.connection.execute(
                "UPDATE corrections SET status='effective',previous_classification=?,previous_sensitive_fields=?,effective_at=? WHERE id=?",
                (
                    source["classification"],
                    json.dumps(previous_sensitive, ensure_ascii=False),
                    at,
                    correction_id,
                ),
            )
            impact = self.impact(correction_id)
            self.connection.execute(
                "UPDATE corrections SET impact_snapshot=? WHERE id=?",
                (json.dumps(impact, ensure_ascii=False, sort_keys=True), correction_id),
            )
            # 用生效后的记录生成说明，确保写明的是实际生效的纠正范围。
            note = self._correction_note(self.get(correction_id))
            for delivery in impact["deliveries"]:
                # 只给在纠正生效时已经作出的凭据追加说明；生效后才发出的
                # 交付本来就按新分级核验，不需要事后纠正注记。
                if delivery["created_at"] > at:
                    continue
                self.connection.execute(
                    "INSERT OR IGNORE INTO delivery_corrections("
                    "correction_id,delivery_id,original_determination,note,recorded_at"
                    ") VALUES(?,?,?,?,?)",
                    (
                        correction_id,
                        delivery["delivery_id"],
                        json.dumps(
                            {
                                "policy_revisions": delivery["policy_revisions"],
                                "input_digest": delivery["input_digest"],
                                "product_id": delivery["product_id"],
                                "product_revision": delivery["product_revision"],
                                "created_at": delivery["created_at"],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        note,
                        at,
                    ),
                )
            write(
                self.connection,
                at,
                actor,
                "correction.effective",
                correction_id,
                {
                    "source": record["source_id"],
                    "from": source["classification"],
                    "to": record["new_classification"],
                    "fields": fields,
                    "inputs": [item["asset_id"] for item in impact["inputs"]],
                    "products": [
                        {"id": item["product_id"], "revision": item["revision"]} for item in impact["products"]
                    ],
                    "recipients": impact["recipients"],
                },
            )
        return self.get(correction_id)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get(self, correction_id):
        row = self.connection.execute("SELECT * FROM corrections WHERE id=?", (correction_id,)).fetchone()
        if row is None:
            raise KeyError(correction_id)
        return self._decode(row)

    def list(self, source_id=None, status=None):
        clauses, params = [], []
        if source_id is not None:
            clauses.append("source_id=?")
            params.append(source_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT * FROM corrections" + where + " ORDER BY proposed_at,rowid", params
        ).fetchall()
        return [self._decode(row) for row in rows]

    def impact(self, correction_id):
        """列出受纠正影响的输入、产品、接收方，以及对应的字段和规则。"""
        record = self.get(correction_id)
        source = self._source(record["source_id"])
        scope_fields = record["sensitive_fields"]
        field_rows = self.connection.execute(
            "SELECT name,meaning,kind,sensitive FROM fields WHERE source_id=? ORDER BY name",
            (record["source_id"],),
        ).fetchall()
        effective = record["status"] == "effective"
        fields = [
            {
                "name": row["name"],
                "meaning": row["meaning"],
                "kind": row["kind"],
                "sensitive": bool(row["sensitive"]),
                "proposed_sensitive": row["name"] in record["proposed_fields"],
                "corrected_sensitive": effective and row["name"] in scope_fields,
            }
            for row in field_rows
        ]
        inputs = self._affected_assets(record)
        products = self._affected_products(inputs)
        deliveries = self._deliveries(products, inputs)
        recipients = sorted({delivery["recipient"] for delivery in deliveries})
        grants = self._grants(record)
        grant_recipients = sorted({grant["recipient"] for grant in grants})
        rules = self._rules(record, deliveries, grants)
        by_purpose = {rule["purpose"]: rule for rule in rules}
        effective_at = record["effective_at"] or record["review_at"] or record["proposed_at"]
        for delivery in deliveries:
            delivery["rule"] = by_purpose[delivery["purpose"]]
            delivery["predates_correction"] = delivery["created_at"] <= effective_at

        return {
            "correction": {
                "id": record["id"],
                "status": record["status"],
                "source": record["source_id"],
                "previous_classification": record["previous_classification"],
                "new_classification": record["new_classification"],
                "proposed_classification": record["proposed_classification"],
                "proposed_fields": record["proposed_fields"],
                "sensitive_fields": scope_fields,
                "proposed_by": record["proposed_by"],
                "proposed_at": record["proposed_at"],
                "reviewer": record["reviewer"],
                "review_note": record["review_note"],
                "review_at": record["review_at"],
                "effective_at": record["effective_at"],
                "note": record["note"],
            },
            "source": {
                "id": source["id"],
                "owner": source["owner"],
                "jurisdiction": source["jurisdiction"],
                "classification": source["classification"],
            },
            "fields": fields,
            "inputs": inputs,
            "products": products,
            "deliveries": deliveries,
            "recipients": recipients,
            "grants": grants,
            "grant_recipients": grant_recipients,
            "rules": rules,
            "summary": {
                "inputs": len(inputs),
                "products": len(products),
                "deliveries": len(deliveries),
                "recipients": len(recipients),
                "grants": len(grants),
            },
        }

    def resolve(self, asset):
        """返回一条输入记录当前适用的分类、敏感字段与生效纠正。

        所有已生效的纠正都会计入，不因交付请求填写了更早的日期而回退到
        旧分级，避免以倒填日期绕过分级纠正。
        """
        source = self._source(asset["source_id"])
        payload = json.loads(asset["payload"])
        classification = source["classification"]
        applied = []
        rows = self.connection.execute(
            "SELECT * FROM corrections WHERE source_id=? AND status='effective' "
            "ORDER BY effective_at,rowid",
            (asset["source_id"],),
        ).fetchall()
        for row in rows:
            scope = json.loads(row["sensitive_fields"])
            # 字段范围纠正只作用于含相关字段的记录，同来源的其他记录以及
            # 其他来源均不受牵连；来源范围纠正（scope 为空）作用于全部记录。
            if not scope or any(name in payload for name in scope):
                classification = row["new_classification"]
                applied.append(row["id"])
        sensitive = [
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM fields WHERE source_id=? AND sensitive=1 ORDER BY name",
                (asset["source_id"],),
            ).fetchall()
            if row["name"] in payload
        ]
        return {
            "classification": classification,
            "corrections": applied,
            "sensitive_fields": sensitive,
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _source(self, source_id):
        row = self.connection.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise KeyError(source_id)
        return dict(row)

    def _normalise_fields(self, source_id, fields):
        names = list(dict.fromkeys(fields or []))
        known = {
            row["name"]
            for row in self.connection.execute("SELECT name FROM fields WHERE source_id=?", (source_id,)).fetchall()
        }
        unknown = sorted(set(names) - known)
        if unknown:
            raise ValueError("纠正字段不在来源目录: " + ",".join(unknown))
        return names

    def _sensitive_field_names(self, source_id):
        return sorted(
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM fields WHERE source_id=? AND sensitive=1", (source_id,)
            ).fetchall()
        )

    def _decode(self, row):
        record = dict(row)
        for key in ("proposed_fields", "sensitive_fields", "previous_sensitive_fields"):
            record[key] = json.loads(record[key]) if record.get(key) else []
        if record.get("impact_snapshot"):
            record["impact_snapshot"] = json.loads(record["impact_snapshot"])
        return record

    def _affected_assets(self, record):
        scope = record["sensitive_fields"]
        result = []
        for row in self.connection.execute(
            "SELECT * FROM assets WHERE source_id=? ORDER BY id", (record["source_id"],)
        ).fetchall():
            payload = json.loads(row["payload"])
            present = sorted(payload)
            if scope and not any(name in payload for name in scope):
                continue
            result.append(
                {
                    "asset_id": row["id"],
                    "source_id": row["source_id"],
                    "digest": row["digest"],
                    "received_at": row["received_at"],
                    "state": row["state"],
                    "fields": present,
                    "sensitive_fields": [
                        name
                        for name in self._sensitive_field_names(record["source_id"])
                        if name in payload
                    ],
                    "correction_fields": [name for name in scope if name in payload],
                }
            )
        return result

    def _affected_products(self, inputs):
        affected = {item["asset_id"]: item for item in inputs}
        products = {}
        for asset_id in affected:
            rows = self.connection.execute(
                "SELECT p.id,p.revision,p.name,p.state,p.created_at "
                "FROM product_inputs pi JOIN products p "
                "ON p.id=pi.product_id AND p.revision=pi.product_revision "
                "WHERE pi.asset_id=?",
                (asset_id,),
            ).fetchall()
            for row in rows:
                key = (row["id"], row["revision"])
                products.setdefault(
                    key,
                    {
                        "product_id": row["id"],
                        "revision": row["revision"],
                        "name": row["name"],
                        "state": row["state"],
                        "created_at": row["created_at"],
                    },
                )
        result = []
        for (product_id, revision), info in sorted(products.items()):
            all_inputs = self.connection.execute(
                "SELECT asset_id FROM product_inputs WHERE product_id=? AND product_revision=? ORDER BY asset_id",
                (product_id, revision),
            ).fetchall()
            input_ids = [row["asset_id"] for row in all_inputs]
            affected_ids = [asset_id for asset_id in input_ids if asset_id in affected]
            fields = sorted(
                {name for asset_id in affected_ids for name in affected[asset_id]["correction_fields"]}
            )
            result.append(
                {
                    **info,
                    "inputs": input_ids,
                    "affected_inputs": affected_ids,
                    "affected_fields": fields,
                }
            )
        return result

    def _deliveries(self, products, inputs):
        wanted = {(item["product_id"], item["revision"]) for item in products}
        affected = {item["asset_id"]: item for item in inputs}
        affected_asset_ids = set(affected)
        result = []
        for row in self.connection.execute("SELECT * FROM deliveries ORDER BY created_at,id").fetchall():
            if (row["product_id"], row["product_revision"]) not in wanted:
                continue
            # 列出该凭据中实际被纠正波及的输入与字段，便于从接收方追到
            # 具体记录和字段。
            hit_ids = [
                item["asset_id"]
                for item in self.connection.execute(
                    "SELECT asset_id FROM product_inputs WHERE product_id=? AND product_revision=? "
                    "ORDER BY asset_id",
                    (row["product_id"], row["product_revision"]),
                ).fetchall()
                if item["asset_id"] in affected_asset_ids
            ]
            result.append(
                {
                    "delivery_id": row["id"],
                    "product_id": row["product_id"],
                    "product_revision": row["product_revision"],
                    "recipient": row["recipient"],
                    "purpose": row["purpose"],
                    "created_at": row["created_at"],
                    "input_digest": row["input_digest"],
                    "policy_revisions": json.loads(row["policy_revisions"]),
                    "affected_inputs": hit_ids,
                    "affected_fields": sorted(
                        {name for asset_id in hit_ids for name in affected[asset_id]["correction_fields"]}
                    ),
                }
            )
        return result

    def _grants(self, record):
        """该来源的许可，说明哪些接收方可能受新分级影响。"""
        rows = self.connection.execute(
            "SELECT id,recipient,purpose,valid_until,status FROM grants "
            "WHERE source_id=? ORDER BY recipient,id",
            (record["source_id"],),
        ).fetchall()
        return [dict(row) for row in rows]

    def _rules(self, record, deliveries, grants):
        """每条规则对应一个受影响的交付目的，并给出纠正后的分级下的判定。

        规则按 ``(法域, 分级, 目的)`` 定位；工作人员可以从接收方经目的追到
        具体规则修订与最终决策。
        """
        source = self._source(record["source_id"])
        purposes = sorted({delivery["purpose"] for delivery in deliveries} | {grant["purpose"] for grant in grants})
        by_purpose = {}
        for row in self.connection.execute(
            "SELECT * FROM policies WHERE jurisdiction=? AND classification=?",
            (source["jurisdiction"], record["new_classification"]),
        ).fetchall():
            by_purpose[row["purpose"]] = dict(row)
        result = []
        for purpose in purposes:
            row = by_purpose.get(purpose)
            result.append(
                {
                    "jurisdiction": source["jurisdiction"],
                    "classification": record["new_classification"],
                    "purpose": purpose,
                    "decision": row["decision"] if row else "deny",
                    "revision": row["revision"] if row else 0,
                    "delivery_ids": sorted(
                        delivery["delivery_id"]
                        for delivery in deliveries
                        if delivery["purpose"] == purpose
                    ),
                    "grant_ids": sorted(
                        grant["id"] for grant in grants if grant["purpose"] == purpose
                    ),
                }
            )
        return result

    def _correction_note(self, record):
        if record["sensitive_fields"]:
            if record["new_classification"] == record["previous_classification"]:
                parts = [
                    "分级纠正 {}：字段 {} 标记为敏感，分级维持 {} 级".format(
                        record["id"],
                        "、".join(record["sensitive_fields"]),
                        record["new_classification"],
                    )
                ]
            else:
                parts = [
                    "分级纠正 {}：含字段 {} 的记录改按 {} 级处理（原登记为 {} 级）".format(
                        record["id"],
                        "、".join(record["sensitive_fields"]),
                        record["new_classification"],
                        record["previous_classification"],
                    )
                ]
        else:
            parts = [
                "分级纠正 {}：该来源全部记录由 {} 级改为 {} 级".format(
                    record["id"], record["previous_classification"], record["new_classification"]
                )
            ]
        if record["note"]:
            parts.append("纠正事由：" + record["note"])
        if record["review_note"]:
            parts.append("审阅说明：" + record["review_note"])
        return "；".join(parts)
