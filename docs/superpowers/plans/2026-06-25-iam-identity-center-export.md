# IAM Identity Center Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a portable Python utility that exports AWS IAM Identity Center account assignments for audit review.

**Architecture:** A single boto3-based CLI queries the IAM Identity Center instance from the management or delegated admin account, lists active AWS Organizations accounts, resolves permission sets and principals, and writes CSV/JSON outputs. The code keeps AWS API calls bounded with low default concurrency, botocore adaptive retries, and local caches for permission sets, users, groups, and group memberships.

**Tech Stack:** Python 3 standard library, boto3/botocore at runtime, unittest with fake clients for local verification.

---

### Task 1: Test Export Row Shaping

**Files:**
- Create: `tests/test_export_identity_center_assignments.py`
- Create: `export_identity_center_assignments.py`

- [ ] Write failing tests for account assignment row generation, user/group display fields, summary counts, and optional group expansion.
- [ ] Run `python3 -m unittest discover -s tests -v` and confirm it fails because the implementation is missing.
- [ ] Implement the minimal exporter logic with fake-client-friendly functions.
- [ ] Run `python3 -m unittest discover -s tests -v` and confirm it passes.

### Task 2: Add Runtime CLI

**Files:**
- Modify: `export_identity_center_assignments.py`

- [ ] Add argparse options for `--profile`, `--region`, `--output-dir`, `--max-workers`, `--expand-groups`, `--include-suspended-accounts`, and `--output-prefix`.
- [ ] Add boto3 session/client creation with botocore retry mode `adaptive` and bounded concurrency.
- [ ] Add CSV and JSON writers for raw assignments, expanded user rows, and account summary.
- [ ] Run unit tests and `python3 -m py_compile export_identity_center_assignments.py`.

### Task 3: Add Operator README

**Files:**
- Create: `README.md`

- [ ] Document where to run the script, required AWS permissions, sample commands, output files, throttling controls, and audit interpretation notes.
- [ ] Run a final verification command set and record any limitations.
