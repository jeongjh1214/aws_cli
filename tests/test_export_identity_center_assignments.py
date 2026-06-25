import unittest

from export_identity_center_assignments import (
    AssignmentExporter,
    build_account_summary,
)


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        for page in self.pages:
            yield page


class FakeSsoAdmin:
    def __init__(self):
        self.permission_set_descriptions = {
            "arn:aws:sso:::permissionSet/ssoins-1/ps-admin": {"Name": "AdminAccess"},
            "arn:aws:sso:::permissionSet/ssoins-1/ps-read": {"Name": "ReadOnly"},
        }
        self.assignments = {
            ("111111111111", "arn:aws:sso:::permissionSet/ssoins-1/ps-admin"): [
                {
                    "AccountId": "111111111111",
                    "PermissionSetArn": "arn:aws:sso:::permissionSet/ssoins-1/ps-admin",
                    "PrincipalId": "user-1",
                    "PrincipalType": "USER",
                },
                {
                    "AccountId": "111111111111",
                    "PermissionSetArn": "arn:aws:sso:::permissionSet/ssoins-1/ps-admin",
                    "PrincipalId": "group-1",
                    "PrincipalType": "GROUP",
                },
            ],
            ("222222222222", "arn:aws:sso:::permissionSet/ssoins-1/ps-read"): [
                {
                    "AccountId": "222222222222",
                    "PermissionSetArn": "arn:aws:sso:::permissionSet/ssoins-1/ps-read",
                    "PrincipalId": "group-1",
                    "PrincipalType": "GROUP",
                }
            ],
        }

    def describe_permission_set(self, **kwargs):
        return {"PermissionSet": self.permission_set_descriptions[kwargs["PermissionSetArn"]]}

    def get_paginator(self, operation_name):
        if operation_name == "list_permission_sets_provisioned_to_account":
            return FakePaginator(
                [
                    {
                        "PermissionSets": [
                            "arn:aws:sso:::permissionSet/ssoins-1/ps-admin",
                        ]
                    }
                ]
            )
        if operation_name == "list_account_assignments":
            return AssignmentPaginator(self.assignments)
        raise AssertionError(f"unexpected paginator: {operation_name}")


class AssignmentPaginator:
    def __init__(self, assignments):
        self.assignments = assignments

    def paginate(self, **kwargs):
        key = (kwargs["AccountId"], kwargs["PermissionSetArn"])
        yield {"AccountAssignments": self.assignments.get(key, [])}


class FakeIdentityStore:
    def __init__(self):
        self.user_describe_calls = 0
        self.group_describe_calls = 0

    def describe_user(self, **kwargs):
        self.user_describe_calls += 1
        users = {
            "user-1": {
                "UserName": "alice",
                "DisplayName": "Alice Kim",
                "Emails": [{"Value": "alice@example.com", "Primary": True}],
            },
            "user-2": {
                "UserName": "bob",
                "DisplayName": "Bob Park",
                "Emails": [{"Value": "bob@example.com"}],
            },
        }
        return users[kwargs["UserId"]]

    def describe_group(self, **kwargs):
        self.group_describe_calls += 1
        return {"DisplayName": "CloudAdmins"}

    def get_paginator(self, operation_name):
        if operation_name == "list_group_memberships":
            return FakePaginator(
                [
                    {
                        "GroupMemberships": [
                            {"MemberId": {"UserId": "user-1"}},
                            {"MemberId": {"UserId": "user-2"}},
                        ]
                    }
                ]
            )
        raise AssertionError(f"unexpected paginator: {operation_name}")


class AssignmentExporterTest(unittest.TestCase):
    def test_export_account_rows_resolves_principals_and_permission_sets(self):
        sso_admin = FakeSsoAdmin()
        identity_store = FakeIdentityStore()
        exporter = AssignmentExporter(
            sso_admin=sso_admin,
            identity_store=identity_store,
            instance_arn="arn:aws:sso:::instance/ssoins-1",
            identity_store_id="d-123",
            expand_groups=False,
        )
        account = {"Id": "111111111111", "Name": "prod", "Status": "ACTIVE"}

        rows, expanded_rows = exporter.export_account(account)

        self.assertEqual(len(expanded_rows), 1)
        self.assertEqual(expanded_rows[0]["effective_user_name"], "alice")
        self.assertEqual(
            rows,
            [
                {
                    "account_id": "111111111111",
                    "account_name": "prod",
                    "account_status": "ACTIVE",
                    "permission_set_arn": "arn:aws:sso:::permissionSet/ssoins-1/ps-admin",
                    "permission_set_name": "AdminAccess",
                    "principal_type": "USER",
                    "principal_id": "user-1",
                    "principal_name": "alice",
                    "principal_display_name": "Alice Kim",
                    "principal_email": "alice@example.com",
                },
                {
                    "account_id": "111111111111",
                    "account_name": "prod",
                    "account_status": "ACTIVE",
                    "permission_set_arn": "arn:aws:sso:::permissionSet/ssoins-1/ps-admin",
                    "permission_set_name": "AdminAccess",
                    "principal_type": "GROUP",
                    "principal_id": "group-1",
                    "principal_name": "CloudAdmins",
                    "principal_display_name": "CloudAdmins",
                    "principal_email": "",
                },
            ],
        )

    def test_expand_groups_outputs_effective_user_rows_and_uses_cache(self):
        sso_admin = FakeSsoAdmin()
        identity_store = FakeIdentityStore()
        exporter = AssignmentExporter(
            sso_admin=sso_admin,
            identity_store=identity_store,
            instance_arn="arn:aws:sso:::instance/ssoins-1",
            identity_store_id="d-123",
            expand_groups=True,
        )

        first_rows, first_expanded_rows = exporter.export_account(
            {"Id": "111111111111", "Name": "prod", "Status": "ACTIVE"}
        )
        second_rows, second_expanded_rows = exporter.export_account(
            {"Id": "222222222222", "Name": "dev", "Status": "ACTIVE"}
        )

        self.assertEqual(len(first_rows), 2)
        self.assertEqual(len(second_rows), 0)
        self.assertEqual(len(first_expanded_rows), 3)
        self.assertEqual(len(second_expanded_rows), 0)
        self.assertEqual(identity_store.group_describe_calls, 1)
        self.assertEqual(identity_store.user_describe_calls, 2)
        group_expanded_rows = [
            row for row in first_expanded_rows if row["source_principal_type"] == "GROUP"
        ]
        self.assertEqual(group_expanded_rows[0]["effective_user_name"], "alice")
        self.assertEqual(group_expanded_rows[0]["source_principal_name"], "CloudAdmins")
        self.assertEqual(group_expanded_rows[1]["effective_user_email"], "bob@example.com")

    def test_build_account_summary_counts_unique_principals(self):
        rows = [
            {
                "account_id": "111111111111",
                "account_name": "prod",
                "account_status": "ACTIVE",
                "permission_set_name": "AdminAccess",
                "principal_type": "USER",
                "principal_id": "user-1",
            },
            {
                "account_id": "111111111111",
                "account_name": "prod",
                "account_status": "ACTIVE",
                "permission_set_name": "AdminAccess",
                "principal_type": "USER",
                "principal_id": "user-1",
            },
            {
                "account_id": "111111111111",
                "account_name": "prod",
                "account_status": "ACTIVE",
                "permission_set_name": "ReadOnly",
                "principal_type": "GROUP",
                "principal_id": "group-1",
            },
        ]
        expanded_rows = [
            {
                "account_id": "111111111111",
                "effective_user_id": "user-1",
            },
            {
                "account_id": "111111111111",
                "effective_user_id": "user-2",
            },
        ]

        self.assertEqual(
            build_account_summary(rows, expanded_rows),
            [
                {
                    "account_id": "111111111111",
                    "account_name": "prod",
                    "account_status": "ACTIVE",
                    "direct_user_count": 1,
                    "group_count": 1,
                    "permission_set_count": 2,
                    "effective_user_count": 2,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
