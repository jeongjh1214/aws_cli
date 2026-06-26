# Weekly Identity Center Org Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a uv-installable CLI that runs a weekly IAM Identity Center audit, enriches users with Krew organization data, stores snapshots in SQLite, and outputs change files for alerting.

**Architecture:** The new CLI reuses the existing IAM Identity Center exporter for live effective-user rows, deduplicates DisplayName values before Krew API calls, persists run snapshots in SQLite, compares the current successful run with the previous successful run, and writes CSV/JSON outputs. Packaging uses `pyproject.toml` console scripts so operators can run it through `uv run` or `uv tool install .`.

**Tech Stack:** Python 3 standard library, boto3/botocore through uv-managed dependencies, SQLite, unittest with fake clients.

---

### Task 1: Tests

**Files:**
- Create: `tests/test_weekly_identity_center_org_audit.py`
- Create: `weekly_identity_center_org_audit.py`

- [ ] Add tests for SQLite run creation, assignment snapshot storage, change detection, and Krew cache deduplication.
- [ ] Run `python3 -m unittest discover -s tests -v` and confirm the new tests fail before implementation.

### Task 2: CLI Implementation

**Files:**
- Create: `weekly_identity_center_org_audit.py`
- Create: `pyproject.toml`

- [ ] Implement SQLite schema, Krew client, per-run DisplayName dedupe, cache TTL, live Identity Center collection, diff generation, and output writers.
- [ ] Add console scripts for existing exporters and the new weekly audit CLI.

### Task 3: Docs and Verification

**Files:**
- Modify: `README.md`
- Create: `.claude/CLAUDE.md`
- Create: `docs/wiki/identity-center-org-audit.md`

- [ ] Document uv install/run usage, operational options, SQLite tables, Krew API cache behavior, and team wiki guidance.
- [ ] Verify tests, py_compile, and uv CLI help before committing and pushing.
