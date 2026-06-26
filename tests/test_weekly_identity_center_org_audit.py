import sqlite3
import tempfile
import unittest
from pathlib import Path

from weekly_identity_center_org_audit import (
    AssignmentSnapshotStore,
    KrewCache,
    build_assignment_key,
    detect_changes,
    enrich_assignments_with_krew,
)


def row(
    account_id,
    permission_set_arn,
    effective_user_id,
    display_name,
    org_code="ORG-A",
    org_name="Cloud",
):
    return {
        "account_id": account_id,
        "account_name": f"account-{account_id}",
        "account_status": "ACTIVE",
        "permission_set_arn": permission_set_arn,
        "permission_set_name": permission_set_arn.rsplit("/", 1)[-1],
        "source_principal_type": "USER",
        "source_principal_id": effective_user_id,
        "source_principal_name": display_name,
        "effective_user_id": effective_user_id,
        "effective_user_name": display_name,
        "effective_user_display_name": display_name,
        "effective_user_email": f"{display_name}@example.com",
        "org_code": org_code,
        "org_name": org_name,
        "krew_status": "OK",
        "krew_error": "",
    }


class FakeKrewClient:
    def __init__(self):
        self.calls = []
        self.people = {
            "billy.j": {"org_code": "ORG-1", "org_name": "Platform"},
            "alice.k": {"org_code": "ORG-2", "org_name": "Security"},
        }

    def fetch_org(self, display_name):
        self.calls.append(display_name)
        return self.people[display_name]


class WeeklyIdentityCenterOrgAuditTest(unittest.TestCase):
    def test_build_assignment_key_uses_stable_identity_fields(self):
        self.assertEqual(
            build_assignment_key(
                row("111111111111", "arn:aws:sso:::permissionSet/ssoins-1/ps-admin", "user-1", "billy.j")
            ),
            "111111111111|arn:aws:sso:::permissionSet/ssoins-1/ps-admin|user-1",
        )

    def test_enrich_assignments_deduplicates_display_names_within_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(Path(tmp) / "audit.sqlite3")
            cache = KrewCache(db, ttl_days=0, refresh=True)
            client = FakeKrewClient()
            rows = [
                row("111111111111", "arn:aws:sso:::permissionSet/ssoins-1/ps-admin", "user-1", "billy.j"),
                row("222222222222", "arn:aws:sso:::permissionSet/ssoins-1/ps-read", "user-1", "billy.j"),
                row("333333333333", "arn:aws:sso:::permissionSet/ssoins-1/ps-dev", "user-2", "alice.k"),
            ]

            enriched, errors = enrich_assignments_with_krew(rows, cache, client)

            self.assertEqual(errors, [])
            self.assertEqual(client.calls, ["billy.j", "alice.k"])
            self.assertEqual(enriched[0]["org_code"], "ORG-1")
            self.assertEqual(enriched[1]["org_name"], "Platform")
            self.assertEqual(enriched[2]["org_code"], "ORG-2")

    def test_detect_changes_finds_added_removed_and_org_changed_rows(self):
        previous = [
            row("111111111111", "arn:aws:sso:::permissionSet/ssoins-1/ps-admin", "user-1", "billy.j", "OLD", "OldOrg"),
            row("222222222222", "arn:aws:sso:::permissionSet/ssoins-1/ps-read", "user-2", "alice.k", "SEC", "Security"),
            row("333333333333", "arn:aws:sso:::permissionSet/ssoins-1/ps-dev", "user-3", "carol.p", "DEV", "Dev"),
        ]
        current = [
            row("111111111111", "arn:aws:sso:::permissionSet/ssoins-1/ps-admin", "user-1", "billy.j", "NEW", "NewOrg"),
            row("222222222222", "arn:aws:sso:::permissionSet/ssoins-1/ps-read", "user-2", "alice.k", "SEC", "Security"),
            row("444444444444", "arn:aws:sso:::permissionSet/ssoins-1/ps-prod", "user-4", "david.l", "PRD", "Prod"),
        ]

        changes = detect_changes(previous, current)

        self.assertEqual([change["change_type"] for change in changes], ["ADDED", "REMOVED", "ORG_CHANGED"])
        self.assertEqual(changes[0]["effective_user_display_name"], "david.l")
        self.assertEqual(changes[1]["effective_user_display_name"], "carol.p")
        self.assertEqual(changes[2]["old_org_code"], "OLD")
        self.assertEqual(changes[2]["new_org_code"], "NEW")

    def test_store_persists_runs_assignments_and_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AssignmentSnapshotStore(Path(tmp) / "audit.sqlite3")
            first_run = store.start_run(source="unit-test")
            store.save_assignments(
                first_run,
                [row("111111111111", "arn:aws:sso:::permissionSet/ssoins-1/ps-admin", "user-1", "billy.j")],
            )
            store.finish_run(first_run, "SUCCESS", "")

            second_run = store.start_run(source="unit-test")
            previous_rows = store.load_assignments_for_run(store.previous_successful_run_id(second_run))
            current_rows = [
                row("111111111111", "arn:aws:sso:::permissionSet/ssoins-1/ps-admin", "user-1", "billy.j"),
                row("222222222222", "arn:aws:sso:::permissionSet/ssoins-1/ps-read", "user-2", "alice.k"),
            ]
            changes = detect_changes(previous_rows, current_rows)
            store.save_assignments(second_run, current_rows)
            store.save_changes(second_run, changes)
            store.finish_run(second_run, "SUCCESS", "")

            self.assertEqual(store.previous_successful_run_id(second_run), first_run)
            self.assertEqual(len(store.load_assignments_for_run(second_run)), 2)
            self.assertEqual(store.load_changes_for_run(second_run)[0]["change_type"], "ADDED")


if __name__ == "__main__":
    unittest.main()
