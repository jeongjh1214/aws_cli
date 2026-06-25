# AWS IAM Identity Center 할당 현황 추출

`export_identity_center_assignments.py`는 AWS IAM Identity Center에 연결된 AWS 계정별 Permission Set 할당 현황을 CSV/JSON으로 추출하는 감사 대응용 스크립트입니다.

## 어디서 실행하나

각 linked account에 접속해서 실행하지 않습니다.

IAM Identity Center 조직 인스턴스를 관리할 수 있는 다음 위치 중 하나에서 실행합니다.

- AWS Organizations management account
- IAM Identity Center delegated admin account

그리고 `--region`은 IAM Identity Center가 활성화된 리전을 지정합니다.

## 설치

Python과 boto3가 필요합니다.

```bash
python3 -m pip install boto3
```

## 실행 예시

기본 추출:

```bash
python3 export_identity_center_assignments.py \
  --profile audit \
  --region ap-northeast-2 \
  --output-dir ./output
```

GROUP 할당을 실제 사용자 목록으로 펼쳐서 추출:

```bash
python3 export_identity_center_assignments.py \
  --profile audit \
  --region ap-northeast-2 \
  --output-dir ./output \
  --expand-groups
```

API throttling이 걱정되면 worker 수를 낮춥니다.

```bash
python3 export_identity_center_assignments.py \
  --profile audit \
  --region ap-northeast-2 \
  --max-workers 2 \
  --max-attempts 15 \
  --output-dir ./output
```

## 출력 파일

기본 prefix는 `identity_center`입니다.

- `identity_center_assignments.csv`: 계정별 Permission Set 원본 할당. USER와 GROUP 할당을 그대로 보여줍니다.
- `identity_center_effective_users.csv`: 실제 사용자 기준 행. 직접 USER 할당은 항상 포함되고, `--expand-groups`를 켜면 GROUP 멤버도 포함됩니다.
- `identity_center_account_summary.csv`: 계정별 요약 카운트.
- `identity_center_errors.csv`: 일부 계정 조회 실패 시 에러 목록. 전체 실행은 가능한 한 계속 진행합니다.
- `identity_center_export.json`: 위 데이터를 모두 포함한 JSON.

CSV는 Excel에서 한글/UTF-8이 깨지지 않도록 UTF-8 BOM으로 저장합니다.

## 감사 해석 팁

감사원이 말한 "계정보유자 숫자, 보유자 리스트"가 IAM Identity Center 기준인지, 실제 사람 기준인지 애매할 수 있습니다.

- `assignments.csv`의 `principal_type=USER`: 특정 사용자에게 직접 부여된 권한입니다.
- `assignments.csv`의 `principal_type=GROUP`: 그룹에 부여된 권한입니다. 이 경우 보유자를 그룹으로 볼 수도 있고, 그룹 멤버를 실제 보유자로 볼 수도 있습니다.
- `effective_users.csv`: 실제 사용자 관점에 더 가깝습니다. 정확한 사람 수가 필요하면 `--expand-groups`로 실행한 결과를 사용하세요.
- `account_summary.csv`의 `direct_user_count`: 계정에 직접 할당된 고유 사용자 수입니다.
- `account_summary.csv`의 `group_count`: 계정에 할당된 고유 그룹 수입니다.
- `account_summary.csv`의 `effective_user_count`: 직접 사용자와, `--expand-groups` 사용 시 그룹 멤버까지 포함한 고유 사용자 수입니다.

## 필요한 IAM 권한

실행 주체에는 최소한 아래 조회 권한이 필요합니다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "organizations:ListAccounts",
        "sso:ListInstances",
        "sso:ListPermissionSetsProvisionedToAccount",
        "sso:ListAccountAssignments",
        "sso:DescribePermissionSet",
        "identitystore:DescribeUser",
        "identitystore:DescribeGroup",
        "identitystore:ListGroupMemberships"
      ],
      "Resource": "*"
    }
  ]
}
```

`identitystore:ListGroupMemberships`는 `--expand-groups`를 사용할 때 필요합니다.

## 속도와 API 제한

기본값은 `--max-workers 4`, `--retry-mode adaptive`, `--max-attempts 12`입니다.

계정이 100개 이상이고 Permission Set이 많아도 linked account별로 로그인하는 방식은 아닙니다. 관리 계정 또는 delegated admin 계정에서 IAM Identity Center와 Organizations API를 조회합니다.

느리거나 throttling이 발생하면 아래 순서로 낮춰서 실행하세요.

```bash
--max-workers 2
--max-workers 1
```

`identity_center_errors.csv`가 비어 있으면 전체 계정 조회가 성공한 것입니다.

---

# AWS Account별 IAM User 추출

`export_iam_users_from_profiles.py`는 `~/.aws/credentials`에 생성된 계정별 profile을 사용해서 각 AWS account의 IAM User 목록을 추출합니다.

IAM Identity Center와 달리 IAM User는 각 계정 안의 IAM API를 조회해야 하므로, 사전에 각 계정으로 접근 가능한 profile이 필요합니다. 이 스크립트는 기본적으로 아래 marker 이후에 있는 profile 중 이름에 `kakaopay-aws`가 포함된 profile만 사용합니다.

```text
# === Org Assume Role Profiles (generated 2026-04-01) ===
```

## 실행 예시

```bash
python3 export_iam_users_from_profiles.py \
  --credentials-file ~/.aws/credentials \
  --profile-prefix kakaopay-aws \
  --output-dir ./output
```

느리거나 API throttling이 있으면 worker 수를 낮춥니다.

```bash
python3 export_iam_users_from_profiles.py \
  --credentials-file ~/.aws/credentials \
  --profile-prefix kakaopay-aws \
  --output-dir ./output \
  --max-workers 1 \
  --max-attempts 15
```

## 출력 파일

- `iam_users.csv`: 계정별 IAM User 요약.
- `iam_users_access_keys.csv`: IAM User별 access key 목록.
- `iam_users_errors.csv`: profile별 조회 실패 목록.
- `iam_users_export.json`: 위 데이터를 모두 포함한 JSON.

`iam_users.csv` 주요 컬럼:

- `profile`: 사용한 AWS CLI profile 이름.
- `account_id`: STS `GetCallerIdentity`로 확인한 AWS 계정 ID.
- `account_name`: profile 이름에서 `kakaopay-aws` prefix를 제거해 추정한 이름.
- `iam_user_name`: IAM User 이름.
- `console_access_enabled`: IAM User에 Login Profile이 있으면 `true`, 없으면 `false`.
- `mfa_enabled`: MFA device가 1개 이상이면 `true`.
- `access_key_count`: access key 총 개수.
- `active_access_key_count`: `Active` 상태 access key 개수.
- `password_last_used`: AWS가 제공하는 IAM User password last used 값.

## 필요한 IAM 권한

각 profile이 접근하는 계정에서 최소한 아래 조회 권한이 필요합니다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "sts:GetCallerIdentity",
        "iam:ListUsers",
        "iam:GetLoginProfile",
        "iam:ListMFADevices",
        "iam:ListAccessKeys"
      ],
      "Resource": "*"
    }
  ]
}
```

## 해석 팁

`console_access_enabled=true`는 해당 IAM User에 콘솔 로그인용 Login Profile, 즉 비밀번호 프로필이 있다는 뜻입니다.

반대로 `console_access_enabled=false`여도 access key가 있으면 CLI/API 접근은 가능할 수 있습니다. 그래서 계정 보유자 감사에서는 `console_access_enabled`, `mfa_enabled`, `active_access_key_count`를 같이 보는 편이 좋습니다.
