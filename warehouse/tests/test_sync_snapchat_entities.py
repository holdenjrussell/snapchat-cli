from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from warehouse import query as warehouse_query  # noqa: E402
from warehouse import sync_snapchat_entities as sync  # noqa: E402


UTC = dt.timezone.utc


def make_state(
    *,
    status: str = "PAUSED",
    delivery: list[str] | None = None,
    review: str | None = "APPROVED",
    daily_budget: float = 2000.0,
    target_cpa: float = 100.0,
    source_updated_at: dt.datetime | None = None,
    observed_at: dt.datetime | None = None,
) -> dict[str, object]:
    observed = observed_at or dt.datetime(2026, 7, 14, 16, 0, tzinfo=UTC)
    updated = source_updated_at or dt.datetime(2026, 7, 14, 15, 0, tzinfo=UTC)
    return {
        "account_id": "account-1",
        "account_key": "default",
        "entity_type": "ad_squad",
        "entity_id": "squad-1",
        "entity_name": "ONE Prospecting",
        "campaign_id": "campaign-1",
        "ad_squad_id": None,
        "configured_status": status,
        "effective_status": "ACTIVE"
        if status == "ACTIVE" and delivery == ["VALID"]
        else status,
        "delivery_status": delivery if delivery is not None else ["INVALID_NOT_ACTIVE"],
        "review_status": review,
        "daily_budget": daily_budget,
        "daily_budget_micro": int(daily_budget * 1_000_000),
        "bid": target_cpa,
        "bid_micro": int(target_cpa * 1_000_000),
        "target_cpa": target_cpa,
        "target_cpa_micro": int(target_cpa * 1_000_000),
        "bid_strategy": "TARGET_COST",
        "source_created_at": dt.datetime(2026, 7, 1, tzinfo=UTC),
        "source_updated_at": updated,
        "raw_payload": {"id": "squad-1", "status": status},
        "first_seen_at": observed,
        "last_seen_at": observed,
    }


class EntityEnvelopeTests(unittest.TestCase):
    def test_unwraps_snap_subrequest_envelopes(self) -> None:
        payload = {
            "ads": [
                {
                    "sub_request_status": "SUCCESS",
                    "ad": {"id": "ad-1", "name": "Winner"},
                }
            ]
        }
        self.assertEqual(
            sync.unwrap_entities(payload, "ads", "ad"),
            [{"id": "ad-1", "name": "Winner"}],
        )

    def test_recent_change_history_contract_is_guarded_select_sql(self) -> None:
        sql = sync.recent_change_history_sql(
            14,
            account_id="account-'quoted",
            warehouse_schema="snap_stage",
        )
        validated = warehouse_query.validate_select_only(sql)
        self.assertIn("account-''quoted", validated)
        self.assertIn(
            'FROM "snap_stage"."snapchat_change_history"',
            validated,
        )
        self.assertIn(
            "field_name IN ('target_cpa', 'daily_budget', 'configured_status')",
            validated,
        )
        self.assertIn("pg_catalog.now()", validated)

    def test_effective_status_rejects_mixed_and_native_active_blockers(self) -> None:
        for delivery in (
            ["VALID", "NOT_DELIVERING_AD_CONTAINS_INVALID_AUDIENCE"],
            ["VALID", "PENDING"],
            ["VALID", "UNKNOWN_FUTURE_STATUS"],
            [],
        ):
            with self.subTest(delivery=delivery):
                self.assertNotEqual(
                    sync.effective_status(
                        {
                            "status": "ACTIVE",
                            "effective_status": "ACTIVE",
                            "review_status": "APPROVED",
                        },
                        delivery,
                    ),
                    "ACTIVE",
                )
        self.assertEqual(
            sync.effective_status(
                {
                    "status": "ACTIVE",
                    "effective_status": "ACTIVE",
                    "review_status": "APPROVED",
                },
                ["VALID"],
            ),
            "ACTIVE",
        )


class SyncPlanningTests(unittest.TestCase):
    def test_first_run_is_baseline_and_second_identical_run_is_idempotent(self) -> None:
        observed = dt.datetime(2026, 7, 14, 16, 0, tzinfo=UTC)
        incoming = make_state(observed_at=observed)

        baseline_states, baseline_events, baseline_count = sync.plan_sync(
            [incoming], [], [], observed_at=observed
        )
        self.assertEqual(baseline_count, 1)
        self.assertEqual(baseline_events, [])

        next_observed = observed + dt.timedelta(hours=1)
        identical = dict(incoming)
        identical["last_seen_at"] = next_observed
        second_states, second_events, second_baselines = sync.plan_sync(
            [identical], baseline_states, [], observed_at=next_observed
        )
        self.assertEqual(second_baselines, 0)
        self.assertEqual(second_events, [])
        self.assertEqual(second_states[0]["first_seen_at"], observed)
        self.assertEqual(second_states[0]["last_seen_at"], next_observed)

    def test_diff_event_key_is_deterministic(self) -> None:
        prior_seen = dt.datetime(2026, 7, 14, 15, 0, tzinfo=UTC)
        observed = dt.datetime(2026, 7, 14, 16, 0, tzinfo=UTC)
        prior = make_state(status="PAUSED", observed_at=prior_seen)
        incoming = make_state(
            status="ACTIVE",
            delivery=["VALID"],
            source_updated_at=dt.datetime(2026, 7, 14, 15, 30, tzinfo=UTC),
            observed_at=observed,
        )
        _, first, _ = sync.plan_sync([incoming], [prior], [], observed_at=observed)
        _, second, _ = sync.plan_sync([incoming], [prior], [], observed_at=observed)
        self.assertEqual(
            [event["event_key"] for event in first],
            [event["event_key"] for event in second],
        )

    def test_classifies_api_manual_and_snap_system_changes(self) -> None:
        prior_seen = dt.datetime(2026, 7, 14, 15, 0, tzinfo=UTC)
        observed = dt.datetime(2026, 7, 14, 16, 0, tzinfo=UTC)
        prior = make_state(
            status="PAUSED",
            daily_budget=1500.0,
            target_cpa=90.0,
            observed_at=prior_seen,
        )
        incoming = make_state(
            status="ACTIVE",
            delivery=["VALID"],
            daily_budget=2000.0,
            target_cpa=100.0,
            source_updated_at=dt.datetime(2026, 7, 14, 15, 30, tzinfo=UTC),
            observed_at=observed,
        )
        audits = []
        for field_name, new_value in (
            ("configured_status", "ACTIVE"),
            ("daily_budget", 2000.0),
            ("bid", 100.0),
            ("target_cpa", 100.0),
        ):
            audit = {
                "timestamp": dt.datetime(2026, 7, 14, 15, 31, tzinfo=UTC),
                "tool": "adsquad.update",
                "entity_type": "ad_squad",
                "entity_id": "squad-1",
                "field_name": field_name,
                "new_value": new_value,
            }
            audit["source_event_key"] = sync.audit_event_key(audit)
            audits.append(audit)

        _, events, _ = sync.plan_sync([incoming], [prior], audits, observed_at=observed)
        sources = {event["field_name"]: event["source"] for event in events}
        self.assertEqual(sources["configured_status"], "AGENT_API")
        self.assertEqual(sources["daily_budget"], "AGENT_API")
        self.assertEqual(sources["bid"], "AGENT_API")
        self.assertEqual(sources["target_cpa"], "AGENT_API")
        self.assertEqual(sources["effective_status"], "SNAP_SYSTEM")
        self.assertEqual(sources["delivery_status"], "SNAP_SYSTEM")

        _, manual_events, _ = sync.plan_sync(
            [incoming], [prior], [], observed_at=observed
        )
        manual_sources = {
            event["field_name"]: event["source"] for event in manual_events
        }
        self.assertEqual(manual_sources["configured_status"], "HUMAN_MANUAL")
        self.assertEqual(manual_sources["daily_budget"], "HUMAN_MANUAL")
        self.assertEqual(manual_sources["bid"], "HUMAN_MANUAL")
        self.assertEqual(manual_sources["target_cpa"], "HUMAN_MANUAL")

    def test_baseline_does_not_invent_change_history_but_later_diff_does(self) -> None:
        first_seen = dt.datetime(2026, 7, 14, 15, 0, tzinfo=UTC)
        baseline = make_state(observed_at=first_seen)
        states, events, count = sync.plan_sync(
            [baseline], [], [], observed_at=first_seen
        )
        self.assertEqual((count, len(events)), (1, 0))

        observed = first_seen + dt.timedelta(hours=1)
        changed = make_state(
            status="ACTIVE",
            delivery=["VALID"],
            source_updated_at=first_seen + dt.timedelta(minutes=30),
            observed_at=observed,
        )
        _, change_events, count = sync.plan_sync(
            [changed], states, [], observed_at=observed
        )
        self.assertEqual(count, 0)
        self.assertGreaterEqual(len(change_events), 2)
        self.assertIn(
            "configured_status", {event["field_name"] for event in change_events}
        )

    def test_older_overlapping_snapshot_cannot_reverse_newer_state(self) -> None:
        older_observed = dt.datetime(2026, 7, 14, 16, 0, tzinfo=UTC)
        newer_observed = older_observed + dt.timedelta(minutes=5)
        newer_state = make_state(
            status="ACTIVE",
            delivery=["VALID"],
            observed_at=newer_observed,
        )
        stale_incoming = make_state(
            status="PAUSED",
            observed_at=older_observed,
        )

        with self.assertRaisesRegex(
            sync.SyncError,
            "Refusing a stale Snapchat snapshot",
        ):
            sync.plan_sync(
                [stale_incoming],
                [newer_state],
                [],
                observed_at=older_observed,
            )

    def test_equal_timestamp_conflict_is_rejected_but_identical_is_idempotent(
        self,
    ) -> None:
        observed = dt.datetime(2026, 7, 14, 16, 0, tzinfo=UTC)
        prior = make_state(status="ACTIVE", delivery=["VALID"], observed_at=observed)
        identical = dict(prior)
        states, events, baselines = sync.plan_sync(
            [identical],
            [prior],
            [],
            observed_at=observed,
        )
        self.assertEqual((events, baselines), ([], 0))
        self.assertEqual(states[0]["last_seen_at"], observed)

        conflict = make_state(status="PAUSED", observed_at=observed)
        with self.assertRaisesRegex(
            sync.SyncError,
            "conflicting Snapchat snapshots with the same observed_at",
        ):
            sync.plan_sync(
                [conflict],
                [prior],
                [],
                observed_at=observed,
            )

    def test_state_upsert_has_database_freshness_guard(self) -> None:
        class Cursor:
            def __init__(self) -> None:
                self.sql = ""
                self.rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def execute(self, sql: str, _row) -> None:
                self.sql = sql
                self.rowcount = 1

        class Connection:
            def __init__(self) -> None:
                self.cursor_instance = Cursor()

            def cursor(self) -> Cursor:
                return self.cursor_instance

        conn = Connection()
        written = sync.upsert_entity_states(
            conn,
            [make_state()],
            warehouse_schema="snap_stage",
        )

        self.assertEqual(written, 1)
        self.assertIn(
            'INSERT INTO "snap_stage"."snapchat_entity_state"',
            conn.cursor_instance.sql,
        )
        self.assertIn(
            "WHERE current_state.last_seen_at < EXCLUDED.last_seen_at",
            conn.cursor_instance.sql,
        )


class SecretSafetyTests(unittest.TestCase):
    def test_local_audit_and_legacy_prior_state_are_scrubbed_before_hash_and_summary(self) -> None:
        audit_canary = "LOCAL_AUDIT_LEAK"
        prior_canary = "PRIOR_STATE_EVENT_LEAK"
        summary_canary = "SUMMARY_LEAK"
        observed = dt.datetime(2026, 7, 14, 16, 0, tzinfo=UTC)

        audit = sync._audit_mutation(
            timestamp=observed,
            tool="adsquad.update",
            entity_type="ad_squad",
            entity_id="squad-1",
            field_name="entity_name",
            raw_value=f"Authorization: Bearer {audit_canary}",
        )
        self.assertIsNotNone(audit)
        self.assertNotIn(audit_canary, json.dumps(audit, default=str))

        prior = make_state(observed_at=observed - dt.timedelta(hours=1))
        prior["entity_name"] = f"Cookie: sid={prior_canary}"
        incoming = make_state(observed_at=observed)
        incoming["entity_name"] = f"Safe name {summary_canary}"
        states, events, baseline_count = sync.plan_sync(
            [incoming],
            [prior],
            [],
            observed_at=observed,
        )
        entity_name_event = next(
            event for event in events if event["field_name"] == "entity_name"
        )
        event_text = json.dumps(entity_name_event, default=str)
        self.assertNotIn(prior_canary, event_text)
        self.assertIn("[REDACTED]", event_text)

        # Make the new modeled name itself secret-shaped to exercise summary.
        events_for_summary = [
            {
                **entity_name_event,
                "new_value": f"api_key={summary_canary}",
                "entity_name": f"Bearer {summary_canary}",
            }
        ]
        summary = sync.summary_payload(
            execute=False,
            account_id="account-1",
            entities=states,
            events=events_for_summary,
            baseline_count=baseline_count,
            audit_event_count=1,
            database_state_loaded=True,
            schema_ready=True,
        )
        rendered_summary = json.dumps(summary, default=str)
        self.assertNotIn(summary_canary, rendered_summary)
        self.assertNotIn(prior_canary, rendered_summary)

    def test_successful_cli_json_is_scrubbed_before_entity_normalization(self) -> None:
        canary = "TOPSECRET-SUCCESS-PAYLOAD"
        response = {
            "campaigns": [{
                "campaign": {
                    "id": "campaign-1",
                    "name": f"Authorization: Bearer {canary}",
                }
            }]
        }
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(response),
            stderr="",
        )
        with mock.patch.object(sync.subprocess, "run", return_value=completed):
            payload = sync.run_cli_json("default", "campaign", "list")

        rendered = json.dumps(payload, sort_keys=True)
        self.assertNotIn(canary, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_raw_payload_and_audit_import_do_not_retain_secrets(self) -> None:
        raw = {
            "id": "squad-1",
            "status": "ACTIVE",
            "access_token": "top-secret-token",
            "nested": {
                "client_secret": "client-secret-value",
                "safe": "keep-me",
            },
        }
        sanitized = sync.sanitize_payload(raw)
        serialized = json.dumps(sanitized)
        self.assertNotIn("top-secret-token", serialized)
        self.assertNotIn("client-secret-value", serialized)
        self.assertIn("keep-me", serialized)

        audit_entry = {
            "ts": "2026-07-14T15:31:00+00:00",
            "tool": "adsquad.update",
            "account": "default",
            "params": {
                "id": "squad-1",
                "fields": {
                    "status": "ACTIVE",
                    "bid_micro": 100_000_000,
                    "access_token": "audit-secret-token",
                    "client_secret": "audit-client-secret",
                },
            },
            "result": "ok",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "audit.jsonl"
            path.write_text(json.dumps(audit_entry) + "\n", encoding="utf-8")
            events = sync.load_local_audit_events(path, account_key="default")

        event_json = json.dumps(events, default=str)
        self.assertEqual(len(events), 3)
        self.assertEqual(
            {event["field_name"] for event in events},
            {"configured_status", "bid", "target_cpa"},
        )
        self.assertNotIn("audit-secret-token", event_json)
        self.assertNotIn("audit-client-secret", event_json)

        error = sync.redact_error_text(
            "postgres://user:db-password@host/db Authorization: Bearer bearer-token "
            '"access_token":"json-token" password=plain-password'
        )
        self.assertNotIn("db-password", error)
        self.assertNotIn("bearer-token", error)
        self.assertNotIn("json-token", error)
        self.assertNotIn("plain-password", error)


if __name__ == "__main__":
    unittest.main()
