#!/usr/bin/env python3
"""Export AWS IAM Identity Center account assignments for audit review."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ASSIGNMENT_FIELDS = [
    "account_id",
    "account_name",
    "account_status",
    "permission_set_arn",
    "permission_set_name",
    "principal_type",
    "principal_id",
    "principal_name",
    "principal_display_name",
    "principal_email",
]

EFFECTIVE_USER_FIELDS = [
    "account_id",
    "account_name",
    "account_status",
    "permission_set_arn",
    "permission_set_name",
    "source_principal_type",
    "source_principal_id",
    "source_principal_name",
    "effective_user_id",
    "effective_user_name",
    "effective_user_display_name",
    "effective_user_email",
]

SUMMARY_FIELDS = [
    "account_id",
    "account_name",
    "account_status",
    "direct_user_count",
    "group_count",
    "permission_set_count",
    "effective_user_count",
]

ERROR_FIELDS = ["account_id", "account_name", "stage", "error_type", "message"]


def paginate(client: Any, operation_name: str, result_key: str, **kwargs: Any) -> list[Any]:
    paginator = client.get_paginator(operation_name)
    items = []
    for page in paginator.paginate(**kwargs):
        items.extend(page.get(result_key, []))
    return items


def preferred_email(user: dict[str, Any]) -> str:
    emails = user.get("Emails") or []
    for email in emails:
        if email.get("Primary"):
            return email.get("Value", "")
    if emails:
        return emails[0].get("Value", "")
    return ""


@dataclass(frozen=True)
class PrincipalInfo:
    principal_id: str
    name: str
    display_name: str
    email: str


class AssignmentExporter:
    def __init__(
        self,
        *,
        sso_admin: Any,
        identity_store: Any,
        instance_arn: str,
        identity_store_id: str,
        expand_groups: bool,
    ) -> None:
        self.sso_admin = sso_admin
        self.identity_store = identity_store
        self.instance_arn = instance_arn
        self.identity_store_id = identity_store_id
        self.expand_groups = expand_groups
        self._permission_set_cache: dict[str, str] = {}
        self._user_cache: dict[str, PrincipalInfo] = {}
        self._group_cache: dict[str, PrincipalInfo] = {}
        self._group_members_cache: dict[str, list[str]] = {}
        self._lock = threading.RLock()

    def export_account(self, account: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        account_id = account["Id"]
        account_name = account.get("Name", "")
        account_status = account.get("Status", "")
        rows: list[dict[str, Any]] = []
        expanded_rows: list[dict[str, Any]] = []

        permission_set_arns = paginate(
            self.sso_admin,
            "list_permission_sets_provisioned_to_account",
            "PermissionSets",
            InstanceArn=self.instance_arn,
            AccountId=account_id,
        )

        for permission_set_arn in permission_set_arns:
            permission_set_name = self.permission_set_name(permission_set_arn)
            assignments = paginate(
                self.sso_admin,
                "list_account_assignments",
                "AccountAssignments",
                InstanceArn=self.instance_arn,
                AccountId=account_id,
                PermissionSetArn=permission_set_arn,
            )
            for assignment in assignments:
                principal_type = assignment["PrincipalType"]
                principal_id = assignment["PrincipalId"]
                principal = self.principal_info(principal_type, principal_id)
                row = {
                    "account_id": account_id,
                    "account_name": account_name,
                    "account_status": account_status,
                    "permission_set_arn": permission_set_arn,
                    "permission_set_name": permission_set_name,
                    "principal_type": principal_type,
                    "principal_id": principal_id,
                    "principal_name": principal.name,
                    "principal_display_name": principal.display_name,
                    "principal_email": principal.email,
                }
                rows.append(row)
                expanded_rows.extend(self.effective_user_rows(row))

        return rows, expanded_rows

    def permission_set_name(self, permission_set_arn: str) -> str:
        with self._lock:
            cached = self._permission_set_cache.get(permission_set_arn)
        if cached is not None:
            return cached

        response = self.sso_admin.describe_permission_set(
            InstanceArn=self.instance_arn,
            PermissionSetArn=permission_set_arn,
        )
        name = response["PermissionSet"].get("Name", permission_set_arn)
        with self._lock:
            self._permission_set_cache[permission_set_arn] = name
        return name

    def principal_info(self, principal_type: str, principal_id: str) -> PrincipalInfo:
        if principal_type == "USER":
            return self.user_info(principal_id)
        if principal_type == "GROUP":
            return self.group_info(principal_id)
        return PrincipalInfo(principal_id, principal_id, principal_id, "")

    def user_info(self, user_id: str) -> PrincipalInfo:
        with self._lock:
            cached = self._user_cache.get(user_id)
        if cached is not None:
            return cached

        response = self.identity_store.describe_user(
            IdentityStoreId=self.identity_store_id,
            UserId=user_id,
        )
        info = PrincipalInfo(
            principal_id=user_id,
            name=response.get("UserName", user_id),
            display_name=response.get("DisplayName") or response.get("UserName", user_id),
            email=preferred_email(response),
        )
        with self._lock:
            self._user_cache[user_id] = info
        return info

    def group_info(self, group_id: str) -> PrincipalInfo:
        with self._lock:
            cached = self._group_cache.get(group_id)
        if cached is not None:
            return cached

        response = self.identity_store.describe_group(
            IdentityStoreId=self.identity_store_id,
            GroupId=group_id,
        )
        name = response.get("DisplayName", group_id)
        info = PrincipalInfo(principal_id=group_id, name=name, display_name=name, email="")
        with self._lock:
            self._group_cache[group_id] = info
        return info

    def group_member_user_ids(self, group_id: str) -> list[str]:
        with self._lock:
            cached = self._group_members_cache.get(group_id)
        if cached is not None:
            return cached

        memberships = paginate(
            self.identity_store,
            "list_group_memberships",
            "GroupMemberships",
            IdentityStoreId=self.identity_store_id,
            GroupId=group_id,
        )
        user_ids = [
            membership["MemberId"]["UserId"]
            for membership in memberships
            if "UserId" in membership.get("MemberId", {})
        ]
        with self._lock:
            self._group_members_cache[group_id] = user_ids
        return user_ids

    def effective_user_rows(self, assignment_row: dict[str, Any]) -> list[dict[str, Any]]:
        if assignment_row["principal_type"] == "USER":
            return [
                {
                    "account_id": assignment_row["account_id"],
                    "account_name": assignment_row["account_name"],
                    "account_status": assignment_row["account_status"],
                    "permission_set_arn": assignment_row["permission_set_arn"],
                    "permission_set_name": assignment_row["permission_set_name"],
                    "source_principal_type": "USER",
                    "source_principal_id": assignment_row["principal_id"],
                    "source_principal_name": assignment_row["principal_name"],
                    "effective_user_id": assignment_row["principal_id"],
                    "effective_user_name": assignment_row["principal_name"],
                    "effective_user_display_name": assignment_row["principal_display_name"],
                    "effective_user_email": assignment_row["principal_email"],
                }
            ]

        if assignment_row["principal_type"] != "GROUP" or not self.expand_groups:
            return []

        effective_rows = []
        for user_id in self.group_member_user_ids(assignment_row["principal_id"]):
            user = self.user_info(user_id)
            effective_rows.append(
                {
                    "account_id": assignment_row["account_id"],
                    "account_name": assignment_row["account_name"],
                    "account_status": assignment_row["account_status"],
                    "permission_set_arn": assignment_row["permission_set_arn"],
                    "permission_set_name": assignment_row["permission_set_name"],
                    "source_principal_type": "GROUP",
                    "source_principal_id": assignment_row["principal_id"],
                    "source_principal_name": assignment_row["principal_name"],
                    "effective_user_id": user.principal_id,
                    "effective_user_name": user.name,
                    "effective_user_display_name": user.display_name,
                    "effective_user_email": user.email,
                }
            )
        return effective_rows


def build_account_summary(
    assignment_rows: list[dict[str, Any]],
    effective_user_rows: list[dict[str, Any]],
    accounts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    account_data: dict[str, dict[str, Any]] = {}

    for account in accounts or []:
        account_id = account["Id"]
        account_data[account_id] = {
            "account_id": account_id,
            "account_name": account.get("Name", ""),
            "account_status": account.get("Status", ""),
            "direct_users": set(),
            "groups": set(),
            "permission_sets": set(),
            "effective_users": set(),
        }

    for row in assignment_rows:
        account_id = row["account_id"]
        data = account_data.setdefault(
            account_id,
            {
                "account_id": account_id,
                "account_name": row.get("account_name", ""),
                "account_status": row.get("account_status", ""),
                "direct_users": set(),
                "groups": set(),
                "permission_sets": set(),
                "effective_users": set(),
            },
        )
        data["permission_sets"].add(row["permission_set_name"])
        if row["principal_type"] == "USER":
            data["direct_users"].add(row["principal_id"])
        elif row["principal_type"] == "GROUP":
            data["groups"].add(row["principal_id"])

    for row in effective_user_rows:
        account_id = row["account_id"]
        data = account_data.setdefault(
            account_id,
            {
                "account_id": account_id,
                "account_name": row.get("account_name", ""),
                "account_status": row.get("account_status", ""),
                "direct_users": set(),
                "groups": set(),
                "permission_sets": set(),
                "effective_users": set(),
            },
        )
        data["effective_users"].add(row["effective_user_id"])

    summary_rows = []
    for account_id in sorted(account_data):
        data = account_data[account_id]
        summary_rows.append(
            {
                "account_id": account_id,
                "account_name": data["account_name"],
                "account_status": data["account_status"],
                "direct_user_count": len(data["direct_users"]),
                "group_count": len(data["groups"]),
                "permission_set_count": len(data["permission_sets"]),
                "effective_user_count": len(data["effective_users"]),
            }
        )
    return summary_rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def load_boto3() -> tuple[Any, Any]:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise SystemExit("boto3/botocore가 필요합니다. 사내 실행 환경에서 `pip install boto3` 후 다시 실행하세요.") from exc
    return boto3, Config


def make_session(profile: str | None, region: str | None) -> Any:
    boto3, _ = load_boto3()
    kwargs = {}
    if profile:
        kwargs["profile_name"] = profile
    if region:
        kwargs["region_name"] = region
    return boto3.Session(**kwargs)


def make_client(
    session: Any,
    service_name: str,
    retry_mode: str,
    max_attempts: int,
    region_name: str | None = None,
) -> Any:
    _, Config = load_boto3()
    return session.client(
        service_name,
        region_name=region_name,
        config=Config(
            retries={
                "mode": retry_mode,
                "max_attempts": max_attempts,
            }
        ),
    )


def discover_instance(sso_admin: Any, instance_arn: str | None, identity_store_id: str | None) -> tuple[str, str]:
    instances = paginate(sso_admin, "list_instances", "Instances")
    if instance_arn:
        matches = [instance for instance in instances if instance.get("InstanceArn") == instance_arn]
        if not matches:
            raise SystemExit(f"지정한 instance arn을 찾지 못했습니다: {instance_arn}")
        instance = matches[0]
    else:
        if not instances:
            raise SystemExit("IAM Identity Center instance를 찾지 못했습니다. 관리 계정/delegated admin 및 리전을 확인하세요.")
        if len(instances) > 1:
            instance_list = "\n".join(instance["InstanceArn"] for instance in instances)
            raise SystemExit(
                "IAM Identity Center instance가 여러 개입니다. --instance-arn을 지정하세요.\n"
                f"{instance_list}"
            )
        instance = instances[0]

    discovered_identity_store_id = instance.get("IdentityStoreId")
    if identity_store_id and identity_store_id != discovered_identity_store_id:
        return instance["InstanceArn"], identity_store_id
    return instance["InstanceArn"], discovered_identity_store_id


def list_accounts(organizations: Any, include_suspended_accounts: bool) -> list[dict[str, Any]]:
    accounts = paginate(organizations, "list_accounts", "Accounts")
    if include_suspended_accounts:
        return sorted(accounts, key=lambda account: account["Id"])
    return sorted(
        [account for account in accounts if account.get("Status") == "ACTIVE"],
        key=lambda account: account["Id"],
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    env_retry_mode = os.environ.get("AWS_RETRY_MODE")
    default_retry_mode = env_retry_mode if env_retry_mode in {"standard", "adaptive"} else "adaptive"
    parser = argparse.ArgumentParser(
        description="Export AWS IAM Identity Center account assignments to CSV and JSON.",
    )
    parser.add_argument("--profile", help="AWS CLI profile name.")
    parser.add_argument("--region", help="IAM Identity Center region, for example ap-northeast-2.")
    parser.add_argument(
        "--organizations-region",
        default="us-east-1",
        help="AWS Organizations client region. Default: us-east-1",
    )
    parser.add_argument("--output-dir", default="output", help="Directory for exported files. Default: output")
    parser.add_argument("--output-prefix", default="identity_center", help="Output filename prefix.")
    parser.add_argument("--max-workers", type=int, default=4, help="Concurrent account workers. Default: 4")
    parser.add_argument("--max-attempts", type=int, default=12, help="Botocore retry max attempts. Default: 12")
    parser.add_argument(
        "--retry-mode",
        choices=["standard", "adaptive"],
        default=default_retry_mode,
        help="Botocore retry mode. Default: adaptive",
    )
    parser.add_argument("--instance-arn", help="Optional IAM Identity Center instance ARN override.")
    parser.add_argument("--identity-store-id", help="Optional Identity Store ID override.")
    parser.add_argument(
        "--expand-groups",
        action="store_true",
        help="Expand GROUP assignments to effective user rows by listing group memberships.",
    )
    parser.add_argument(
        "--include-suspended-accounts",
        action="store_true",
        help="Include suspended AWS Organizations accounts in the summary/export scan.",
    )
    return parser.parse_args(argv)


def export_all(args: argparse.Namespace) -> dict[str, Path]:
    session = make_session(args.profile, args.region)
    sso_admin = make_client(session, "sso-admin", args.retry_mode, args.max_attempts)
    identity_store = make_client(session, "identitystore", args.retry_mode, args.max_attempts)
    organizations = make_client(
        session,
        "organizations",
        args.retry_mode,
        args.max_attempts,
        region_name=args.organizations_region,
    )

    instance_arn, identity_store_id = discover_instance(
        sso_admin,
        args.instance_arn,
        args.identity_store_id,
    )
    accounts = list_accounts(organizations, args.include_suspended_accounts)
    exporter = AssignmentExporter(
        sso_admin=sso_admin,
        identity_store=identity_store,
        instance_arn=instance_arn,
        identity_store_id=identity_store_id,
        expand_groups=args.expand_groups,
    )

    assignment_rows: list[dict[str, Any]] = []
    effective_user_rows: list[dict[str, Any]] = []
    error_rows: list[dict[str, Any]] = []
    max_workers = max(1, args.max_workers)

    print(f"IAM Identity Center instance: {instance_arn}", file=sys.stderr)
    print(f"Identity Store ID: {identity_store_id}", file=sys.stderr)
    print(f"Accounts to scan: {len(accounts)} / max workers: {max_workers}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_account = {
            executor.submit(exporter.export_account, account): account
            for account in accounts
        }
        completed = 0
        for future in as_completed(future_to_account):
            account = future_to_account[future]
            completed += 1
            try:
                rows, expanded_rows = future.result()
                assignment_rows.extend(rows)
                effective_user_rows.extend(expanded_rows)
                print(
                    f"[{completed}/{len(accounts)}] {account['Id']} {account.get('Name', '')}: "
                    f"{len(rows)} assignments",
                    file=sys.stderr,
                )
            except Exception as exc:  # noqa: BLE001 - keep exporting other accounts for audit collection.
                error_rows.append(
                    {
                        "account_id": account.get("Id", ""),
                        "account_name": account.get("Name", ""),
                        "stage": "export_account",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                print(
                    f"[{completed}/{len(accounts)}] ERROR {account.get('Id', '')} "
                    f"{account.get('Name', '')}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    assignment_rows.sort(
        key=lambda row: (
            row["account_id"],
            row["permission_set_name"],
            row["principal_type"],
            row["principal_name"],
        )
    )
    effective_user_rows.sort(
        key=lambda row: (
            row["account_id"],
            row["permission_set_name"],
            row["effective_user_name"],
            row["source_principal_name"],
        )
    )
    summary_rows = build_account_summary(assignment_rows, effective_user_rows, accounts)
    generated_at = datetime.now(timezone.utc).isoformat()
    output_dir = Path(args.output_dir)
    prefix = args.output_prefix

    paths = {
        "assignments_csv": output_dir / f"{prefix}_assignments.csv",
        "effective_users_csv": output_dir / f"{prefix}_effective_users.csv",
        "summary_csv": output_dir / f"{prefix}_account_summary.csv",
        "errors_csv": output_dir / f"{prefix}_errors.csv",
        "json": output_dir / f"{prefix}_export.json",
    }

    write_csv(paths["assignments_csv"], assignment_rows, ASSIGNMENT_FIELDS)
    write_csv(paths["effective_users_csv"], effective_user_rows, EFFECTIVE_USER_FIELDS)
    write_csv(paths["summary_csv"], summary_rows, SUMMARY_FIELDS)
    write_csv(paths["errors_csv"], error_rows, ERROR_FIELDS)
    write_json(
        paths["json"],
        {
            "generated_at": generated_at,
            "instance_arn": instance_arn,
            "identity_store_id": identity_store_id,
            "account_count": len(accounts),
            "assignment_count": len(assignment_rows),
            "effective_user_row_count": len(effective_user_rows),
            "error_count": len(error_rows),
            "assignments": assignment_rows,
            "effective_users": effective_user_rows,
            "account_summary": summary_rows,
            "errors": error_rows,
        },
    )
    return paths


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    paths = export_all(args)
    print("Export complete.", file=sys.stderr)
    for label, path in paths.items():
        print(f"{label}: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
