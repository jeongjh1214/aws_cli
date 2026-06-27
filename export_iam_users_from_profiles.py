#!/usr/bin/env python3
"""Export IAM users from generated AWS CLI profiles for audit review."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MARKER = "# === Org Assume Role Profiles (generated 2026-04-01) ==="
DEFAULT_PROFILE_PREFIX = "aws"

IAM_USER_FIELDS = [
    "profile",
    "account_id",
    "account_name",
    "iam_user_name",
    "iam_user_arn",
    "user_id",
    "create_date",
    "password_last_used",
    "console_access_enabled",
    "login_profile_create_date",
    "mfa_enabled",
    "mfa_device_count",
    "access_key_count",
    "active_access_key_count",
]

ACCESS_KEY_FIELDS = [
    "profile",
    "account_id",
    "account_name",
    "iam_user_name",
    "access_key_id",
    "access_key_status",
    "access_key_create_date",
]

ERROR_FIELDS = ["profile", "account_id", "account_name", "stage", "iam_user_name", "error_type", "message"]


def paginate(client: Any, operation_name: str, result_key: str, **kwargs: Any) -> list[Any]:
    paginator = client.get_paginator(operation_name)
    rows = []
    for page in paginator.paginate(**kwargs):
        rows.extend(page.get(result_key, []))
    return rows


def isoformat(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def is_no_such_entity(exc: Exception) -> bool:
    if "NoSuchEntity" in exc.__class__.__name__:
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code") == "NoSuchEntity"
    return False


def normalize_profile_name(section_name: str) -> str:
    section_name = section_name.strip()
    if section_name.startswith("profile "):
        return section_name[len("profile ") :].strip()
    return section_name


def parse_profiles_from_credentials_text(text: str, marker: str, profile_prefix: str) -> list[str]:
    marker_seen = False
    profiles: list[str] = []
    seen = set()
    section_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")

    for line in text.splitlines():
        if not marker_seen:
            if line.strip() == marker:
                marker_seen = True
            continue

        match = section_re.match(line)
        if not match:
            continue

        profile = normalize_profile_name(match.group(1))
        if profile_prefix in profile and profile not in seen:
            profiles.append(profile)
            seen.add(profile)

    return profiles


def load_profiles_from_credentials_file(path: Path, marker: str, profile_prefix: str) -> list[str]:
    text = path.expanduser().read_text(encoding="utf-8")
    return parse_profiles_from_credentials_text(text, marker, profile_prefix)


def guess_account_name(profile: str, profile_prefix: str) -> str:
    if profile == profile_prefix:
        return profile
    if profile.startswith(profile_prefix):
        suffix = profile[len(profile_prefix) :].lstrip("-_ ")
        return suffix or profile
    return profile


class IamUserExporter:
    def __init__(self, *, iam: Any) -> None:
        self.iam = iam

    def export_profile(
        self,
        *,
        profile: str,
        account_id: str,
        account_name: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        users = paginate(self.iam, "list_users", "Users")
        user_rows = []
        access_key_rows = []

        for user in users:
            user_name = user["UserName"]
            login_profile = self.login_profile(user_name)
            mfa_devices = paginate(self.iam, "list_mfa_devices", "MFADevices", UserName=user_name)
            access_keys = paginate(self.iam, "list_access_keys", "AccessKeyMetadata", UserName=user_name)
            active_access_keys = [key for key in access_keys if key.get("Status") == "Active"]

            user_rows.append(
                {
                    "profile": profile,
                    "account_id": account_id,
                    "account_name": account_name,
                    "iam_user_name": user_name,
                    "iam_user_arn": user.get("Arn", ""),
                    "user_id": user.get("UserId", ""),
                    "create_date": isoformat(user.get("CreateDate")),
                    "password_last_used": isoformat(user.get("PasswordLastUsed")),
                    "console_access_enabled": "true" if login_profile else "false",
                    "login_profile_create_date": isoformat((login_profile or {}).get("CreateDate")),
                    "mfa_enabled": "true" if mfa_devices else "false",
                    "mfa_device_count": len(mfa_devices),
                    "access_key_count": len(access_keys),
                    "active_access_key_count": len(active_access_keys),
                }
            )

            for key in access_keys:
                access_key_rows.append(
                    {
                        "profile": profile,
                        "account_id": account_id,
                        "account_name": account_name,
                        "iam_user_name": user_name,
                        "access_key_id": key.get("AccessKeyId", ""),
                        "access_key_status": key.get("Status", ""),
                        "access_key_create_date": isoformat(key.get("CreateDate")),
                    }
                )

        return user_rows, access_key_rows

    def login_profile(self, user_name: str) -> dict[str, Any] | None:
        try:
            return self.iam.get_login_profile(UserName=user_name).get("LoginProfile", {})
        except Exception as exc:
            if is_no_such_entity(exc):
                return None
            raise


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


def make_session(profile: str, region: str | None) -> Any:
    boto3, _ = load_boto3()
    kwargs = {"profile_name": profile}
    if region:
        kwargs["region_name"] = region
    return boto3.Session(**kwargs)


def make_client(session: Any, service_name: str, retry_mode: str, max_attempts: int) -> Any:
    _, Config = load_boto3()
    return session.client(
        service_name,
        config=Config(retries={"mode": retry_mode, "max_attempts": max_attempts}),
    )


def caller_account_id(sts: Any) -> str:
    return sts.get_caller_identity()["Account"]


def export_one_profile(
    *,
    profile: str,
    account_name: str,
    region: str | None,
    retry_mode: str,
    max_attempts: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    try:
        session = make_session(profile, region)
        sts = make_client(session, "sts", retry_mode, max_attempts)
        iam = make_client(session, "iam", retry_mode, max_attempts)
        account_id = caller_account_id(sts)
        exporter = IamUserExporter(iam=iam)
        user_rows, access_key_rows = exporter.export_profile(
            profile=profile,
            account_id=account_id,
            account_name=account_name,
        )
        return user_rows, access_key_rows, errors
    except Exception as exc:  # noqa: BLE001 - keep exporting other profiles for audit collection.
        errors.append(
            {
                "profile": profile,
                "account_id": "",
                "account_name": account_name,
                "stage": "export_profile",
                "iam_user_name": "",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )
        return [], [], errors


def parse_args(argv: list[str]) -> argparse.Namespace:
    env_retry_mode = os.environ.get("AWS_RETRY_MODE")
    default_retry_mode = env_retry_mode if env_retry_mode in {"standard", "adaptive"} else "adaptive"
    parser = argparse.ArgumentParser(
        description="Export IAM users from generated AWS CLI profiles to CSV and JSON.",
    )
    parser.add_argument(
        "--credentials-file",
        default="~/.aws/credentials",
        help="AWS credentials file to scan. Default: ~/.aws/credentials",
    )
    parser.add_argument(
        "--credentials-marker",
        default=DEFAULT_MARKER,
        help="Only profiles after this exact marker line are scanned.",
    )
    parser.add_argument(
        "--profile-prefix",
        default=DEFAULT_PROFILE_PREFIX,
        help="Profile name substring to include. Default: aws",
    )
    parser.add_argument("--region", default="ap-northeast-2", help="Session region. Default: ap-northeast-2")
    parser.add_argument("--output-dir", default="output", help="Directory for exported files. Default: output")
    parser.add_argument("--output-prefix", default="iam_users", help="Output filename prefix.")
    parser.add_argument("--max-workers", type=int, default=4, help="Concurrent profile workers. Default: 4")
    parser.add_argument("--max-attempts", type=int, default=12, help="Botocore retry max attempts. Default: 12")
    parser.add_argument(
        "--retry-mode",
        choices=["standard", "adaptive"],
        default=default_retry_mode,
        help="Botocore retry mode. Default: adaptive",
    )
    return parser.parse_args(argv)


def export_all(args: argparse.Namespace) -> dict[str, Path]:
    credentials_file = Path(args.credentials_file).expanduser()
    profiles = load_profiles_from_credentials_file(
        credentials_file,
        args.credentials_marker,
        args.profile_prefix,
    )
    if not profiles:
        raise SystemExit(
            f"대상 profile을 찾지 못했습니다. file={credentials_file}, "
            f"marker={args.credentials_marker!r}, prefix={args.profile_prefix!r}"
        )

    user_rows: list[dict[str, Any]] = []
    access_key_rows: list[dict[str, Any]] = []
    error_rows: list[dict[str, Any]] = []
    max_workers = max(1, args.max_workers)

    print(f"Credentials file: {credentials_file}", file=sys.stderr)
    print(f"Profiles to scan: {len(profiles)} / max workers: {max_workers}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_profile = {}
        for profile in profiles:
            account_name = guess_account_name(profile, args.profile_prefix)
            future = executor.submit(
                export_one_profile,
                profile=profile,
                account_name=account_name,
                region=args.region,
                retry_mode=args.retry_mode,
                max_attempts=args.max_attempts,
            )
            future_to_profile[future] = profile

        completed = 0
        for future in as_completed(future_to_profile):
            profile = future_to_profile[future]
            completed += 1
            rows, key_rows, errors = future.result()
            user_rows.extend(rows)
            access_key_rows.extend(key_rows)
            error_rows.extend(errors)
            if errors:
                print(f"[{completed}/{len(profiles)}] ERROR {profile}: {errors[0]['message']}", file=sys.stderr)
            else:
                print(f"[{completed}/{len(profiles)}] {profile}: {len(rows)} IAM users", file=sys.stderr)

    user_rows.sort(key=lambda row: (row["account_id"], row["iam_user_name"]))
    access_key_rows.sort(key=lambda row: (row["account_id"], row["iam_user_name"], row["access_key_id"]))
    error_rows.sort(key=lambda row: (row["profile"], row["stage"], row["iam_user_name"]))

    output_dir = Path(args.output_dir)
    prefix = args.output_prefix
    paths = {
        "users_csv": output_dir / f"{prefix}.csv",
        "access_keys_csv": output_dir / f"{prefix}_access_keys.csv",
        "errors_csv": output_dir / f"{prefix}_errors.csv",
        "json": output_dir / f"{prefix}_export.json",
    }

    generated_at = datetime.now(timezone.utc).isoformat()
    write_csv(paths["users_csv"], user_rows, IAM_USER_FIELDS)
    write_csv(paths["access_keys_csv"], access_key_rows, ACCESS_KEY_FIELDS)
    write_csv(paths["errors_csv"], error_rows, ERROR_FIELDS)
    write_json(
        paths["json"],
        {
            "generated_at": generated_at,
            "credentials_file": str(credentials_file),
            "profile_prefix": args.profile_prefix,
            "profile_count": len(profiles),
            "iam_user_count": len(user_rows),
            "access_key_count": len(access_key_rows),
            "error_count": len(error_rows),
            "iam_users": user_rows,
            "access_keys": access_key_rows,
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
