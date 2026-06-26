# aws_cli Audit Tools Guide

This repository contains small Python audit CLIs for AWS account and IAM Identity Center review. Keep changes boring, testable, and compatible with `uv`.

## Commands

Use `uv` for local development and operator-facing execution.

```bash
uv sync
uv run python -m unittest discover -s tests -v
uv run python -m py_compile export_identity_center_assignments.py export_iam_users_from_profiles.py weekly_identity_center_org_audit.py
uv run identity-center-org-audit --help
```

Console scripts are defined in `pyproject.toml`:

- `identity-center-export`
- `iam-users-export`
- `identity-center-org-audit`

## Files

- `export_identity_center_assignments.py`: live IAM Identity Center assignment exporter.
- `export_iam_users_from_profiles.py`: IAM User exporter using generated AWS CLI profiles.
- `weekly_identity_center_org_audit.py`: weekly batch CLI that stores live Identity Center snapshots in SQLite, enriches users through Krew API, and detects changes.
- `tests/`: unittest-based tests with fake clients. Do not require AWS or Krew network access.
- `docs/wiki/identity-center-org-audit.md`: team-facing operation/design document.

## Weekly Audit Design Notes

`weekly_identity_center_org_audit.py` should treat AWS IAM Identity Center as the live source of truth and SQLite as the history store.

The stable permission comparison key is:

```text
account_id + permission_set_arn + effective_user_id
```

Do not use DisplayName as the change detection key. DisplayName is only used for Krew API lookup.

Krew API calls must be deduplicated per run by `effective_user_display_name`. A user can appear in many AWS accounts and Permission Sets; calling the Krew API once per assignment is a bug.

SQLite tables:

- `runs`: one row per batch run.
- `assignments`: one snapshot row per effective user assignment.
- `krew_cache`: DisplayName to org metadata cache.
- `changes`: diff rows for alerting.

## Testing Expectations

When changing batch logic:

1. Add or update tests first.
2. Verify the test fails for the missing/old behavior.
3. Implement the minimal fix.
4. Run the full unittest suite.

Important behavior to preserve:

- `ADDED`, `REMOVED`, and `ORG_CHANGED` diff semantics.
- Krew lookup deduplication within a run.
- Krew cache TTL and `--refresh-krew-cache` behavior.
- `--expand-groups` default remains enabled for weekly audit.

## Secrets

Never commit Krew API keys, AWS credentials, generated SQLite databases, or output CSVs.

Use:

```bash
export KREW_API_KEY="..."
```

The CLI reads the variable name from `--krew-api-key-env`, defaulting to `KREW_API_KEY`.
