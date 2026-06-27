import unittest
from datetime import datetime, timezone

from export_iam_users_from_profiles import (
    IamUserExporter,
    guess_account_name,
    is_no_such_entity,
    parse_profiles_from_credentials_text,
)


MARKER = "# === Org Assume Role Profiles (generated 2026-04-01) ==="


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        for page in self.pages:
            yield page


class FakeNoSuchEntity(Exception):
    pass


class FakeIam:
    class exceptions:
        NoSuchEntityException = FakeNoSuchEntity

    def __init__(self):
        self.users = [
            {
                "UserName": "alice",
                "UserId": "AIDAALICE",
                "Arn": "arn:aws:iam::111111111111:user/alice",
                "CreateDate": datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
                "PasswordLastUsed": datetime(2024, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
            },
            {
                "UserName": "bob",
                "UserId": "AIDABOB",
                "Arn": "arn:aws:iam::111111111111:user/bob",
                "CreateDate": datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc),
            },
        ]

    def get_paginator(self, operation_name):
        if operation_name == "list_users":
            return FakePaginator([{"Users": self.users}])
        if operation_name == "list_mfa_devices":
            return UserScopedPaginator(
                {
                    "alice": [{"SerialNumber": "arn:aws:iam::111111111111:mfa/alice"}],
                    "bob": [],
                },
                "MFADevices",
            )
        if operation_name == "list_access_keys":
            return UserScopedPaginator(
                {
                    "alice": [
                        {
                            "AccessKeyId": "AKIAALICEACTIVE",
                            "Status": "Active",
                            "CreateDate": datetime(2024, 3, 4, 5, 6, 7, tzinfo=timezone.utc),
                        },
                        {
                            "AccessKeyId": "AKIAALICEINACTIVE",
                            "Status": "Inactive",
                            "CreateDate": datetime(2024, 4, 5, 6, 7, 8, tzinfo=timezone.utc),
                        },
                    ],
                    "bob": [],
                },
                "AccessKeyMetadata",
            )
        raise AssertionError(f"unexpected paginator: {operation_name}")

    def get_login_profile(self, **kwargs):
        if kwargs["UserName"] == "alice":
            return {
                "LoginProfile": {
                    "UserName": "alice",
                    "CreateDate": datetime(2024, 1, 3, 4, 5, 6, tzinfo=timezone.utc),
                }
            }
        raise FakeNoSuchEntity("login profile not found")


class UserScopedPaginator:
    def __init__(self, rows_by_user, result_key):
        self.rows_by_user = rows_by_user
        self.result_key = result_key

    def paginate(self, **kwargs):
        yield {self.result_key: self.rows_by_user[kwargs["UserName"]]}


class IamUserExportTest(unittest.TestCase):
    def test_parse_profiles_after_marker_by_prefix(self):
        text = f"""
[default]
aws_access_key_id = before

[company-aws-before-marker]
role_arn = arn:aws:iam::000000000000:role/AuditRole

{MARKER}
[company-aws-prod]
role_arn = arn:aws:iam::111111111111:role/AuditRole

[unrelated]
role_arn = arn:aws:iam::222222222222:role/AuditRole

[profile company-aws-dev]
role_arn = arn:aws:iam::333333333333:role/AuditRole
"""

        self.assertEqual(
            parse_profiles_from_credentials_text(text, MARKER, "company-aws"),
            ["company-aws-prod", "company-aws-dev"],
        )

    def test_guess_account_name_strips_prefix(self):
        self.assertEqual(guess_account_name("company-aws-prod", "company-aws"), "prod")
        self.assertEqual(guess_account_name("company-aws", "company-aws"), "company-aws")

    def test_export_profile_users_shows_console_mfa_and_access_keys(self):
        exporter = IamUserExporter(iam=FakeIam())

        user_rows, access_key_rows = exporter.export_profile(
            profile="company-aws-prod",
            account_id="111111111111",
            account_name="prod",
        )

        self.assertEqual(
            user_rows,
            [
                {
                    "profile": "company-aws-prod",
                    "account_id": "111111111111",
                    "account_name": "prod",
                    "iam_user_name": "alice",
                    "iam_user_arn": "arn:aws:iam::111111111111:user/alice",
                    "user_id": "AIDAALICE",
                    "create_date": "2024-01-02T03:04:05+00:00",
                    "password_last_used": "2024-02-03T04:05:06+00:00",
                    "console_access_enabled": "true",
                    "login_profile_create_date": "2024-01-03T04:05:06+00:00",
                    "mfa_enabled": "true",
                    "mfa_device_count": 1,
                    "access_key_count": 2,
                    "active_access_key_count": 1,
                },
                {
                    "profile": "company-aws-prod",
                    "account_id": "111111111111",
                    "account_name": "prod",
                    "iam_user_name": "bob",
                    "iam_user_arn": "arn:aws:iam::111111111111:user/bob",
                    "user_id": "AIDABOB",
                    "create_date": "2024-05-06T07:08:09+00:00",
                    "password_last_used": "",
                    "console_access_enabled": "false",
                    "login_profile_create_date": "",
                    "mfa_enabled": "false",
                    "mfa_device_count": 0,
                    "access_key_count": 0,
                    "active_access_key_count": 0,
                },
            ],
        )
        self.assertEqual(len(access_key_rows), 2)
        self.assertEqual(access_key_rows[0]["access_key_id"], "AKIAALICEACTIVE")
        self.assertEqual(access_key_rows[0]["access_key_status"], "Active")

    def test_no_such_entity_detection_handles_named_exception_and_client_error_shape(self):
        self.assertTrue(is_no_such_entity(FakeNoSuchEntity("missing")))

        class ClientErrorLike(Exception):
            response = {"Error": {"Code": "NoSuchEntity"}}

        self.assertTrue(is_no_such_entity(ClientErrorLike("missing")))


if __name__ == "__main__":
    unittest.main()
