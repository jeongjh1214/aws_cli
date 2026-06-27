#!/usr/bin/env python3
"""Weekly IAM Identity Center audit with Krew organization enrichment."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from export_identity_center_assignments import (
    AssignmentExporter,
    discover_instance,
    list_accounts,
    make_client,
    make_session,
)


DEFAULT_KREW_API_BASE_URL = ""

SNAPSHOT_FIELDS = [
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
    "org_code",
    "org_name",
    "krew_status",
    "krew_error",
]

CHANGE_FIELDS = [
    "change_type",
    "account_id",
    "account_name",
    "permission_set_arn",
    "permission_set_name",
    "effective_user_id",
    "effective_user_name",
    "effective_user_display_name",
    "effective_user_email",
    "old_org_code",
    "new_org_code",
    "old_org_name",
    "new_org_name",
]

ERROR_FIELDS = ["stage", "subject", "error_type", "message"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def build_assignment_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            row.get("account_id", ""),
            row.get("permission_set_arn", ""),
            row.get("effective_user_id", ""),
        ]
    )


def normalize_snapshot_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {field: row.get(field, "") for field in SNAPSHOT_FIELDS}
    normalized["assignment_key"] = build_assignment_key(normalized)
    return normalized


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


class KrewOrgClient:
    def __init__(self, *, base_url: str, api_key: str, timeout_seconds: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def fetch_org(self, display_name: str) -> dict[str, str]:
        encoded_name = urllib.parse.quote(display_name, safe="")
        request = urllib.request.Request(
            f"{self.base_url}/{encoded_name}",
            headers={"X-API-Key": self.api_key, "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {
                "org_code": "",
                "org_name": "",
                "status": "ERROR",
                "error_message": f"HTTP {exc.code}",
            }
        except Exception as exc:  # noqa: BLE001 - keep batch running and store the failed lookup.
            return {
                "org_code": "",
                "org_name": "",
                "status": "ERROR",
                "error_message": f"{type(exc).__name__}: {exc}",
            }

        return parse_krew_org_payload(payload)


def parse_krew_org_payload(payload: Any) -> dict[str, str]:
    if payload is None:
        return {
            "org_code": "",
            "org_name": "",
            "status": "ERROR",
            "error_message": "unexpected Krew response: null",
        }
    if not isinstance(payload, dict):
        return {
            "org_code": "",
            "org_name": "",
            "status": "ERROR",
            "error_message": f"unexpected Krew response type: {type(payload).__name__}",
        }

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return {
            "org_code": "",
            "org_name": "",
            "status": "ERROR",
            "error_message": f"unexpected Krew data type: {type(data).__name__}",
        }

    main_position = data.get("mainPosition") or {}
    if not isinstance(main_position, dict):
        return {
            "org_code": "",
            "org_name": "",
            "status": "ERROR",
            "error_message": f"unexpected Krew mainPosition type: {type(main_position).__name__}",
        }

    return {
        "org_code": main_position.get("orgCode", "") or "",
        "org_name": main_position.get("orgName", "") or "",
        "status": "OK",
        "error_message": "",
    }


def ensure_sqlite_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
          run_id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          status TEXT NOT NULL,
          source TEXT NOT NULL,
          error_message TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS assignments (
          run_id INTEGER NOT NULL,
          assignment_key TEXT NOT NULL,
          account_id TEXT NOT NULL,
          account_name TEXT NOT NULL,
          account_status TEXT NOT NULL,
          permission_set_arn TEXT NOT NULL,
          permission_set_name TEXT NOT NULL,
          source_principal_type TEXT NOT NULL,
          source_principal_id TEXT NOT NULL,
          source_principal_name TEXT NOT NULL,
          effective_user_id TEXT NOT NULL,
          effective_user_name TEXT NOT NULL,
          effective_user_display_name TEXT NOT NULL,
          effective_user_email TEXT NOT NULL,
          org_code TEXT NOT NULL,
          org_name TEXT NOT NULL,
          krew_status TEXT NOT NULL,
          krew_error TEXT NOT NULL,
          PRIMARY KEY (run_id, assignment_key)
        );

        CREATE TABLE IF NOT EXISTS krew_cache (
          display_name TEXT PRIMARY KEY,
          org_code TEXT NOT NULL,
          org_name TEXT NOT NULL,
          fetched_at TEXT NOT NULL,
          status TEXT NOT NULL,
          error_message TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS changes (
          change_id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id INTEGER NOT NULL,
          change_type TEXT NOT NULL,
          assignment_key TEXT NOT NULL,
          account_id TEXT NOT NULL,
          account_name TEXT NOT NULL,
          permission_set_arn TEXT NOT NULL,
          permission_set_name TEXT NOT NULL,
          effective_user_id TEXT NOT NULL,
          effective_user_name TEXT NOT NULL,
          effective_user_display_name TEXT NOT NULL,
          effective_user_email TEXT NOT NULL,
          old_org_code TEXT NOT NULL,
          new_org_code TEXT NOT NULL,
          old_org_name TEXT NOT NULL,
          new_org_name TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, run_id);
        CREATE INDEX IF NOT EXISTS idx_assignments_run ON assignments(run_id);
        CREATE INDEX IF NOT EXISTS idx_changes_run ON changes(run_id);
        """
    )
    conn.commit()


class AssignmentSnapshotStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        ensure_sqlite_schema(self.conn)

    def start_run(self, source: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO runs(started_at, status, source, error_message) VALUES (?, ?, ?, ?)",
            (iso_now(), "RUNNING", source, ""),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, error_message: str) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, error_message = ? WHERE run_id = ?",
            (iso_now(), status, error_message, run_id),
        )
        self.conn.commit()

    def previous_successful_run_id(self, current_run_id: int) -> int | None:
        row = self.conn.execute(
            """
            SELECT run_id FROM runs
            WHERE status = 'SUCCESS' AND run_id < ?
            ORDER BY run_id DESC
            LIMIT 1
            """,
            (current_run_id,),
        ).fetchone()
        return int(row["run_id"]) if row else None

    def save_assignments(self, run_id: int, rows: list[dict[str, Any]]) -> None:
        normalized_rows = [normalize_snapshot_row(row) for row in rows]
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO assignments(
              run_id, assignment_key, account_id, account_name, account_status,
              permission_set_arn, permission_set_name, source_principal_type,
              source_principal_id, source_principal_name, effective_user_id,
              effective_user_name, effective_user_display_name, effective_user_email,
              org_code, org_name, krew_status, krew_error
            ) VALUES (
              :run_id, :assignment_key, :account_id, :account_name, :account_status,
              :permission_set_arn, :permission_set_name, :source_principal_type,
              :source_principal_id, :source_principal_name, :effective_user_id,
              :effective_user_name, :effective_user_display_name, :effective_user_email,
              :org_code, :org_name, :krew_status, :krew_error
            )
            """,
            [dict(row, run_id=run_id) for row in normalized_rows],
        )
        self.conn.commit()

    def load_assignments_for_run(self, run_id: int | None) -> list[dict[str, Any]]:
        if run_id is None:
            return []
        rows = self.conn.execute(
            "SELECT * FROM assignments WHERE run_id = ? ORDER BY account_id, permission_set_name, effective_user_name",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def save_changes(self, run_id: int, rows: list[dict[str, Any]]) -> None:
        self.conn.executemany(
            """
            INSERT INTO changes(
              run_id, change_type, assignment_key, account_id, account_name,
              permission_set_arn, permission_set_name, effective_user_id,
              effective_user_name, effective_user_display_name, effective_user_email,
              old_org_code, new_org_code, old_org_name, new_org_name
            ) VALUES (
              :run_id, :change_type, :assignment_key, :account_id, :account_name,
              :permission_set_arn, :permission_set_name, :effective_user_id,
              :effective_user_name, :effective_user_display_name, :effective_user_email,
              :old_org_code, :new_org_code, :old_org_name, :new_org_name
            )
            """,
            [dict(row, run_id=run_id) for row in rows],
        )
        self.conn.commit()

    def load_changes_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM changes WHERE run_id = ? ORDER BY change_id",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]


class KrewCache:
    def __init__(self, conn: sqlite3.Connection, *, ttl_days: int, refresh: bool) -> None:
        self.conn = conn
        self.ttl_days = ttl_days
        self.refresh = refresh
        ensure_sqlite_schema(self.conn)

    def get(self, display_name: str) -> dict[str, str] | None:
        if self.refresh:
            return None
        row = self.conn.execute(
            "SELECT * FROM krew_cache WHERE display_name = ?",
            (display_name,),
        ).fetchone()
        if not row:
            return None

        fetched_at = datetime.fromisoformat(row["fetched_at"])
        if fetched_at < now_utc() - timedelta(days=self.ttl_days):
            return None
        return {
            "org_code": row["org_code"],
            "org_name": row["org_name"],
            "status": row["status"],
            "error_message": row["error_message"],
        }

    def set(self, display_name: str, result: dict[str, str]) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO krew_cache(
              display_name, org_code, org_name, fetched_at, status, error_message
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                display_name,
                result.get("org_code", ""),
                result.get("org_name", ""),
                iso_now(),
                result.get("status", "OK"),
                result.get("error_message", ""),
            ),
        )
        self.conn.commit()


def enrich_assignments_with_krew(
    rows: list[dict[str, Any]],
    cache: KrewCache,
    client: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    run_cache: dict[str, dict[str, str]] = {}
    errors: list[dict[str, str]] = []
    enriched_rows = []

    for row in rows:
        display_name = row.get("effective_user_display_name") or row.get("effective_user_name", "")
        if not display_name:
            result = {"org_code": "", "org_name": "", "status": "ERROR", "error_message": "missing display name"}
        elif display_name in run_cache:
            result = run_cache[display_name]
        else:
            cached = cache.get(display_name)
            if cached:
                result = cached
            else:
                result = client.fetch_org(display_name)
                result.setdefault("status", "OK")
                result.setdefault("error_message", "")
                cache.set(display_name, result)
            run_cache[display_name] = result

        if result.get("status") != "OK":
            errors.append(
                {
                    "stage": "krew_lookup",
                    "subject": display_name,
                    "error_type": result.get("status", "ERROR"),
                    "message": result.get("error_message", ""),
                }
            )

        enriched = dict(row)
        enriched["org_code"] = result.get("org_code", "")
        enriched["org_name"] = result.get("org_name", "")
        enriched["krew_status"] = result.get("status", "OK")
        enriched["krew_error"] = result.get("error_message", "")
        enriched_rows.append(enriched)

    return enriched_rows, errors


def change_row(change_type: str, row: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = previous or {}
    return {
        "change_type": change_type,
        "assignment_key": build_assignment_key(row),
        "account_id": row.get("account_id", ""),
        "account_name": row.get("account_name", ""),
        "permission_set_arn": row.get("permission_set_arn", ""),
        "permission_set_name": row.get("permission_set_name", ""),
        "effective_user_id": row.get("effective_user_id", ""),
        "effective_user_name": row.get("effective_user_name", ""),
        "effective_user_display_name": row.get("effective_user_display_name", ""),
        "effective_user_email": row.get("effective_user_email", ""),
        "old_org_code": previous.get("org_code", ""),
        "new_org_code": row.get("org_code", ""),
        "old_org_name": previous.get("org_name", ""),
        "new_org_name": row.get("org_name", ""),
    }


def detect_changes(previous_rows: list[dict[str, Any]], current_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous_by_key = {build_assignment_key(row): row for row in previous_rows}
    current_by_key = {build_assignment_key(row): row for row in current_rows}
    changes: list[dict[str, Any]] = []

    for key in sorted(current_by_key.keys() - previous_by_key.keys()):
        changes.append(change_row("ADDED", current_by_key[key]))

    for key in sorted(previous_by_key.keys() - current_by_key.keys()):
        previous = previous_by_key[key]
        removed = dict(previous, org_code="", org_name="")
        changes.append(change_row("REMOVED", removed, previous))

    for key in sorted(current_by_key.keys() & previous_by_key.keys()):
        previous = previous_by_key[key]
        current = current_by_key[key]
        if previous.get("org_code", "") != current.get("org_code", "") or previous.get("org_name", "") != current.get(
            "org_name", ""
        ):
            changes.append(change_row("ORG_CHANGED", current, previous))

    return changes


def collect_identity_center_effective_users(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
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
    instance_arn, identity_store_id = discover_instance(sso_admin, args.instance_arn, args.identity_store_id)
    accounts = filter_accounts_for_scan(
        list_accounts(organizations, args.include_suspended_accounts),
        account_ids=args.account_id,
        account_name_contains=args.account_name_contains,
        max_accounts=args.max_accounts,
    )
    if not accounts:
        raise SystemExit("스캔 대상 계정이 없습니다. --account-id 또는 --account-name-contains 조건을 확인하세요.")
    exporter = AssignmentExporter(
        sso_admin=sso_admin,
        identity_store=identity_store,
        instance_arn=instance_arn,
        identity_store_id=identity_store_id,
        expand_groups=args.expand_groups,
    )

    print(f"IAM Identity Center instance: {instance_arn}", file=sys.stderr)
    print(f"Accounts to scan: {len(accounts)} / max workers: {args.max_workers}", file=sys.stderr)

    effective_rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as executor:
        future_to_account = {executor.submit(exporter.export_account, account): account for account in accounts}
        completed = 0
        for future in as_completed(future_to_account):
            account = future_to_account[future]
            completed += 1
            try:
                _, expanded_rows = future.result()
                effective_rows.extend(expanded_rows)
                print(
                    f"[{completed}/{len(accounts)}] {account['Id']} {account.get('Name', '')}: "
                    f"{len(expanded_rows)} effective user rows",
                    file=sys.stderr,
                )
            except Exception as exc:  # noqa: BLE001 - keep collecting other accounts.
                errors.append(
                    {
                        "stage": "identity_center_account",
                        "subject": f"{account.get('Id', '')} {account.get('Name', '')}",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                print(
                    f"[{completed}/{len(accounts)}] ERROR {account.get('Id', '')} "
                    f"{account.get('Name', '')}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    effective_rows.sort(
        key=lambda row: (
            row.get("account_id", ""),
            row.get("permission_set_name", ""),
            row.get("effective_user_display_name", ""),
        )
    )
    return effective_rows, errors


def filter_accounts_for_scan(
    accounts: list[dict[str, Any]],
    *,
    account_ids: list[str] | None,
    account_name_contains: str | None,
    max_accounts: int | None,
) -> list[dict[str, Any]]:
    filtered = list(accounts)
    if account_ids:
        wanted = set(account_ids)
        filtered = [account for account in filtered if account.get("Id") in wanted]
    if account_name_contains:
        needle = account_name_contains.lower()
        filtered = [account for account in filtered if needle in account.get("Name", "").lower()]
    if max_accounts is not None:
        if max_accounts < 1:
            raise SystemExit("--max-accounts는 1 이상이어야 합니다.")
        filtered = filtered[:max_accounts]
    return filtered


def output_paths(output_dir: Path, prefix: str) -> dict[str, Path]:
    return {
        "snapshot_csv": output_dir / f"{prefix}_current_snapshot.csv",
        "changes_csv": output_dir / f"{prefix}_changes.csv",
        "errors_csv": output_dir / f"{prefix}_errors.csv",
        "summary_json": output_dir / f"{prefix}_summary.json",
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    env_retry_mode = os.environ.get("AWS_RETRY_MODE")
    default_retry_mode = env_retry_mode if env_retry_mode in {"standard", "adaptive"} else "adaptive"
    parser = argparse.ArgumentParser(
        description="Run weekly IAM Identity Center organization audit and save snapshots to SQLite.",
    )
    parser.add_argument("--profile", help="AWS CLI profile name for IAM Identity Center admin access.")
    parser.add_argument("--region", required=True, help="IAM Identity Center region, for example ap-northeast-2.")
    parser.add_argument("--organizations-region", default="us-east-1", help="Organizations region. Default: us-east-1")
    parser.add_argument("--db", default="./identity_center_audit.sqlite3", help="SQLite database path.")
    parser.add_argument("--output-dir", default="output", help="Directory for generated CSV/JSON files.")
    parser.add_argument("--output-prefix", default="weekly", help="Output filename prefix.")
    parser.add_argument("--krew-api-key-env", default="KREW_API_KEY", help="Env var containing Krew API key.")
    parser.add_argument(
        "--krew-api-base-url",
        default=os.environ.get("KREW_API_BASE_URL", DEFAULT_KREW_API_BASE_URL),
        help="Krew API base URL. Can also be set with KREW_API_BASE_URL.",
    )
    parser.add_argument("--krew-timeout-seconds", type=int, default=10, help="Krew API timeout. Default: 10")
    parser.add_argument("--krew-cache-ttl-days", type=int, default=6, help="Krew cache TTL. Default: 6")
    parser.add_argument("--refresh-krew-cache", action="store_true", help="Ignore Krew cache and refetch all users.")
    parser.add_argument("--max-workers", type=int, default=4, help="Concurrent AWS account workers. Default: 4")
    parser.add_argument(
        "--account-id",
        action="append",
        help="Scan only this AWS account ID. Repeat the option to scan multiple accounts.",
    )
    parser.add_argument(
        "--account-name-contains",
        help="Scan only accounts whose Organizations account name contains this text.",
    )
    parser.add_argument(
        "--max-accounts",
        type=int,
        help="Scan only the first N accounts after filters. Useful for smoke tests.",
    )
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
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Expand GROUP assignments to effective users. Default: true",
    )
    parser.add_argument(
        "--include-suspended-accounts",
        action="store_true",
        help="Include suspended AWS Organizations accounts in the scan.",
    )
    return parser.parse_args(argv)


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    api_key = os.environ.get(args.krew_api_key_env)
    if not api_key:
        raise SystemExit(f"{args.krew_api_key_env} 환경변수에 Krew API key를 설정하세요.")
    if not args.krew_api_base_url:
        raise SystemExit("KREW_API_BASE_URL 환경변수 또는 --krew-api-base-url 옵션을 설정하세요.")

    store = AssignmentSnapshotStore(Path(args.db))
    run_id = store.start_run(source="identity-center-live")
    paths = output_paths(Path(args.output_dir), args.output_prefix)
    errors: list[dict[str, str]] = []

    try:
        current_rows, aws_errors = collect_identity_center_effective_users(args)
        errors.extend(aws_errors)
        krew_cache = KrewCache(store.conn, ttl_days=args.krew_cache_ttl_days, refresh=args.refresh_krew_cache)
        krew_client = KrewOrgClient(
            base_url=args.krew_api_base_url,
            api_key=api_key,
            timeout_seconds=args.krew_timeout_seconds,
        )
        enriched_rows, krew_errors = enrich_assignments_with_krew(current_rows, krew_cache, krew_client)
        errors.extend(krew_errors)

        previous_run_id = store.previous_successful_run_id(run_id)
        previous_rows = store.load_assignments_for_run(previous_run_id)
        changes = detect_changes(previous_rows, enriched_rows)

        store.save_assignments(run_id, enriched_rows)
        store.save_changes(run_id, changes)
        store.finish_run(run_id, "SUCCESS", "")

        write_csv(paths["snapshot_csv"], [normalize_snapshot_row(row) for row in enriched_rows], SNAPSHOT_FIELDS)
        write_csv(paths["changes_csv"], changes, CHANGE_FIELDS)
        write_csv(paths["errors_csv"], errors, ERROR_FIELDS)
        write_json(
            paths["summary_json"],
            {
                "run_id": run_id,
                "previous_run_id": previous_run_id,
                "snapshot_count": len(enriched_rows),
                "change_count": len(changes),
                "error_count": len(errors),
                "changes_by_type": {
                    change_type: sum(1 for change in changes if change["change_type"] == change_type)
                    for change_type in ["ADDED", "REMOVED", "ORG_CHANGED"]
                },
                "paths": {key: str(path) for key, path in paths.items()},
            },
        )
        return {"run_id": run_id, "paths": paths, "changes": changes, "errors": errors}
    except Exception as exc:
        store.finish_run(run_id, "FAILED", str(exc))
        raise


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = run_audit(args)
    print(f"Run complete: {result['run_id']}", file=sys.stderr)
    for label, path in result["paths"].items():
        print(f"{label}: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
