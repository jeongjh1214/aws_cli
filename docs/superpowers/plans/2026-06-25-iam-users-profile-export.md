# IAM Users Profile Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a portable Python utility that reads generated AWS CLI profiles and exports IAM users per account for audit review.

**Architecture:** A separate boto3-based CLI parses `~/.aws/credentials` after the configured marker, selects profiles containing the configured prefix, then queries each profile's account with STS and IAM. The script writes IAM user, access key, error, and JSON outputs while keeping concurrency low for IAM API throttling.

**Tech Stack:** Python 3 standard library, boto3/botocore at runtime, unittest with fake clients for local verification.

---

### Task 1: Profile Parser and IAM User Row Tests

**Files:**
- Create: `tests/test_export_iam_users_from_profiles.py`
- Create: `export_iam_users_from_profiles.py`

- [ ] Write tests for marker-based profile discovery from credentials text.
- [ ] Write tests for IAM user export rows including console login, MFA, and access key counts.
- [ ] Run `python3 -m unittest discover -s tests -v` and confirm the new tests fail because the module is missing.
- [ ] Implement parser and exporter functions until tests pass.

### Task 2: Runtime CLI and Outputs

**Files:**
- Modify: `export_iam_users_from_profiles.py`

- [ ] Add argparse options for credentials path, marker, profile prefix, output directory, output prefix, max workers, retry mode, and max attempts.
- [ ] Add boto3 session/client creation with adaptive retry and per-profile error isolation.
- [ ] Write `iam_users.csv`, `iam_user_access_keys.csv`, `iam_user_errors.csv`, and `iam_users_export.json`.

### Task 3: Documentation and Verification

**Files:**
- Modify: `README.md`

- [ ] Document how to run the IAM user exporter using generated company AWS profiles.
- [ ] Document required permissions and interpretation of `console_access_enabled`.
- [ ] Run unit tests, py_compile, and CLI help verification before committing and pushing.
