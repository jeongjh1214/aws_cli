# IAM Identity Center 조직정보 주간 감사 배치

## 목적

AWS 계정과 Permission Set이 Terraform 등으로 계속 변경되기 때문에, 최초 CSV만으로는 현재 권한 상태를 보장할 수 없다. 이 배치는 매주 IAM Identity Center를 live로 조회하고, 사용자별 사내 조직정보를 붙여 변경분을 만든다.

알람 시스템은 이 배치가 만든 `weekly_changes.csv` 또는 SQLite `changes` 테이블을 읽으면 된다.

## 해결하려는 문제

- AWS linked account가 많아 수동 감사가 어렵다.
- 한 사용자가 여러 AWS 계정/Permission Set에 중복으로 나타난다.
- 사내 조직정보 API를 assignment row마다 호출하면 불필요한 부하가 생긴다.
- 권한 변경과 조직 이동을 주간 단위로 추적해야 한다.
- 감사 대응을 위해 과거 snapshot을 보관해야 한다.

## 전체 흐름

```text
주간 배치 실행
  -> IAM Identity Center live 조회
  -> GROUP assignment를 실제 사용자 기준으로 확장
  -> DisplayName unique 목록 생성
  -> Krew API로 조직정보 조회
  -> SQLite krew_cache에 조직정보 저장
  -> SQLite assignments에 이번 run snapshot 저장
  -> 직전 SUCCESS run과 비교
  -> SQLite changes와 weekly_changes.csv 생성
  -> 알람 시스템이 changes 결과를 사용
```

## 실행 명령

개발/운영 환경에서 `uv`를 사용한다.

```bash
git clone https://github.com/jeongjh1214/aws_cli.git
cd aws_cli
uv sync
```

배치 실행:

```bash
export KREW_API_KEY="발급받은 key"

uv run identity-center-org-audit \
  --profile audit \
  --region ap-northeast-2 \
  --db ./identity_center_audit.sqlite3 \
  --output-dir ./output
```

전역 tool로 설치해서 실행할 수도 있다.

```bash
uv tool install .

identity-center-org-audit \
  --profile audit \
  --region ap-northeast-2 \
  --db ./identity_center_audit.sqlite3 \
  --output-dir ./output
```

## AWS 권한

배치 실행 profile은 IAM Identity Center 관리 계정 또는 delegated admin 계정에서 실행해야 한다.

필요 권한:

```text
organizations:ListAccounts
sso:ListInstances
sso:ListPermissionSetsProvisionedToAccount
sso:ListAccountAssignments
sso:DescribePermissionSet
identitystore:DescribeUser
identitystore:DescribeGroup
identitystore:ListGroupMemberships
```

## Krew API

배치는 IAM Identity Center의 `effective_user_display_name`을 Krew API path parameter로 사용한다.

```text
GET https://knock-api.kakaopay.com/papi/v1/krew/{displayName}
Header: X-API-Key: ...
```

예:

```text
GET /papi/v1/krew/billy.j
```

저장하는 응답 필드:

```text
data.mainPosition.orgCode
data.mainPosition.orgName
```

## API 호출 최적화

한 사용자가 여러 AWS 계정에 있어도 Krew API는 같은 run에서 한 번만 호출한다.

예:

```text
billy.j가 30개 assignment에 있음
  -> Krew API 1회 호출
  -> 30개 assignment row에 같은 orgCode/orgName 적용
```

SQLite `krew_cache`는 기본 6일 TTL로 재사용된다.

```bash
--krew-cache-ttl-days 6
```

조직정보를 강제로 다시 조회하려면:

```bash
--refresh-krew-cache
```

## 변경 감지 기준

권한 비교 key:

```text
account_id + permission_set_arn + effective_user_id
```

DisplayName은 Krew API 조회용으로만 사용한다. 동명이인이나 표시명 변경 가능성이 있으므로 권한 변경 비교 key로 쓰지 않는다.

변경 타입:

```text
ADDED
  새로 권한이 생긴 사용자

REMOVED
  기존 권한이 제거된 사용자

ORG_CHANGED
  권한은 그대로지만 orgCode 또는 orgName이 바뀐 사용자
```

## 출력 파일

기본 output prefix는 `weekly`다.

```text
weekly_current_snapshot.csv
weekly_changes.csv
weekly_errors.csv
weekly_summary.json
```

알람 연동 대상:

```text
weekly_changes.csv
SQLite changes table
```

## SQLite 테이블

`runs`

배치 실행 이력이다.

```text
run_id, started_at, finished_at, status, source, error_message
```

`assignments`

run별 전체 snapshot이다.

```text
run_id, assignment_key, account_id, account_name, permission_set_arn,
permission_set_name, source_principal_type, source_principal_name,
effective_user_id, effective_user_display_name, effective_user_email,
org_code, org_name, krew_status, krew_error
```

`krew_cache`

DisplayName별 조직정보 캐시다.

```text
display_name, org_code, org_name, fetched_at, status, error_message
```

`changes`

직전 정상 run 대비 변경분이다.

```text
run_id, change_type, assignment_key, account_id, account_name,
permission_set_name, effective_user_display_name, old_org_code,
new_org_code, old_org_name, new_org_name
```

## 운영 팁

첫 실행은 비교 대상이 없기 때문에 대부분 `ADDED`로 나올 수 있다. 알람은 두 번째 정상 run부터 활성화하는 것을 권장한다.

Krew API 장애가 있어도 배치는 가능한 한 진행하고 `weekly_errors.csv`와 `krew_status`, `krew_error`에 실패를 기록한다.

AWS throttling이 보이면 worker 수를 낮춘다.

```bash
--max-workers 2
--max-workers 1
```

## 전체 실행 전 smoke test

전체 계정을 다시 조회하기 전에 1개 계정만 별도 DB/output으로 테스트한다. 운영 DB와 섞지 않기 위해 `--db`와 `--output-dir`은 smoke test 전용 값을 사용한다.

```bash
export KREW_API_KEY="발급받은 key"

uv run identity-center-org-audit \
  --profile audit \
  --region ap-northeast-2 \
  --db ./smoke_identity_center_audit.sqlite3 \
  --output-dir ./smoke-output \
  --account-id 123456789012 \
  --max-workers 1
```

계정명을 일부만 알고 있으면:

```bash
uv run identity-center-org-audit \
  --profile audit \
  --region ap-northeast-2 \
  --db ./smoke_identity_center_audit.sqlite3 \
  --output-dir ./smoke-output \
  --account-name-contains sec_engineering-dev \
  --max-workers 1
```

## cron 예시

```cron
0 8 * * 1 cd /path/to/aws_cli && KREW_API_KEY=... uv run identity-center-org-audit --profile audit --region ap-northeast-2 --db ./identity_center_audit.sqlite3 --output-dir ./output >> ./output/weekly.log 2>&1
```
