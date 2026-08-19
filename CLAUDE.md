# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

CI/CD pipeline (CloudFormation + CodePipeline + CodeBuild) that centrally deploys **local AWS Bedrock Guardrails** (the `AWS::Bedrock::Guardrail` resource, created in each target account — NOT organizational guardrails) to specific AWS accounts/OUs via CloudFormation **StackSets** (`SERVICE_MANAGED`, `CallAs=DELEGATED_ADMIN`), governed from a central "SecDevOps" account. This is infrastructure-as-code only — there is no application code, build system, or test framework; "testing" means template linting/security scanning (`cfn-lint`, `checkov`).

**Multi-guardrail model, single target per guardrail**: this repo can manage several distinct guardrails at once. Each guardrail declares exactly **one** deployment target (an OU and/or specific accounts) in its own `targets.json` — there is **no dev/qa/prod environment concept** driving where a guardrail goes. If a guardrail needs to apply somewhere else too, that's a separate guardrail directory, not a second environment of the same one.

## Landing Zone (this deployment)

- SecDevOps account (pipeline + StackSets delegated admin): `474632925684`
- Organizations management account: `758626604929`
- OU DEV: `ou-p9fr-dqd5qn16` — only OU used today (the `general` guardrail targets it; this is not a "dev environment", just the one OU with workload accounts in this Landing Zone)
- OU SECDEVOPS: `ou-p9fr-f0v77vbs`
- Region: `us-east-1`

## Repo layout

- `guardrails/<nombre>/` — **one directory per guardrail** (e.g. `guardrails/general/`). No manual registry anywhere — `scripts/deploy_stackset.py` auto-discovers any subdirectory of `guardrails/` containing both files below.
  - `template.yaml` — `AWS::Bedrock::Guardrail` + `AWS::Bedrock::GuardrailVersion` + SSM Parameters (`/security/bedrock/guardrail/<nombre>/id|version` — the guardrail name is part of the SSM path, never break this shape). Takes `GuardrailName` (must match the directory name) and `EnvironmentTag` (a free descriptive string used only for the `Environment` tag — it does NOT determine deployment target).
  - `targets.json` — single-target config: `stackset_name` (one StackSet per guardrail, no env suffix), `organizational_unit_ids` (always), optional `account_ids` + `account_filter_type` (`INTERSECTION`/`DIFFERENCE`) to narrow to specific accounts within an OU, `regions`, template `parameters`, and `require_approval` (bool, default `false`) — decides whether this guardrail deploys in the pipeline's `Deploy` stage (immediate) or `DeployApproved` stage (after the manual approval gate).
- `pipeline/pipeline.yaml` — bootstrap CloudFormation template for the pipeline itself (S3 artifact bucket + dedicated access-logs bucket, IAM roles, CodeBuild projects, CodePipeline: Source → Validate → Deploy → ApproveDeploy (manual/SNS) → DeployApproved). Deployed **once, manually** into the SecDevOps account.
- `bootstrap/delegated-admin.md` — AWS CLI commands (not CloudFormation — Organizations-level action) to run once in the management account to register SecDevOps as StackSets delegated admin and enable trusted access.
- `bootstrap/codeconnections.yaml` — creates the `AWS::CodeStarConnections::Connection` to GitHub in the SecDevOps account. Must be deployed before `pipeline/pipeline.yaml`; approve manually in console (PENDING → AVAILABLE), then pass its ARN as `GitHubConnectionArn`. The connection must live in the **same account as the pipeline** (SecDevOps).
- `bootstrap/github-oidc-role.yaml` — optional: `github-actions-validate-role` (read-only, `cloudformation:ValidateTemplate` only) for the optional `aws-validate` job in `pr-validate.yml`. Trust policy's `StringLike` on `sub` accepts **both** `repo:<org>/<repo>:*` and `repo:<org>@*/<repo>@*:*` — GitHub appends immutable owner/repo IDs to the `sub` claim (anti-hijacking protection after rename/transfer) and the plain-name-only pattern will silently `AccessDenied` on `AssumeRoleWithWebIdentity` otherwise. Has a `CreateOidcProvider` toggle since an account can only have one `token.actions.githubusercontent.com` OIDC provider.
- `buildspecs/validate.yaml` — CodeBuild spec: `cfn-lint guardrails/*/template.yaml pipeline/pipeline.yaml`, `checkov -d guardrails`/`-d pipeline`, loops `aws cloudformation validate-template` and JSON validation over every `guardrails/*/template.yaml` / `guardrails/*/targets.json`.
- `buildspecs/deploy.yaml` — CodeBuild spec: runs `scripts/deploy_stackset.py --stage "${DEPLOY_STAGE}"` (`auto` or `approved`, injected per pipeline stage via `EnvironmentVariables` in `pipeline/pipeline.yaml`).
- `scripts/deploy_stackset.py` — idempotent, multi-guardrail: discovers `guardrails/*/`, filters by each guardrail's `require_approval` vs. the requested `--stage`, creates/updates each matching StackSet and its stack instances, polling `describe_stack_set_operation` until `SUCCEEDED`/`FAILED`/`STOPPED` and hard-failing the build on failure/stop.
- `.github/workflows/pr-validate.yml` — PR-time shift-left checks only (lint/checkov/JSON validation via GitHub Actions, glob over `guardrails/*/`), plus an optional `cloudformation validate-template` job via GitHub OIDC. **Deployment must never move to GitHub Actions** — it lives exclusively in AWS CodePipeline.

## Architecture / flow

1. PR opened → GitHub Actions (`pr-validate.yml`) runs `cfn-lint` + `checkov` + JSON validation.
2. Merge to `main` → CodeConnections triggers CodePipeline in SecDevOps automatically via GitHub webhook.
3. **Validate** stage repeats lint/scan inside AWS.
4. **Deploy**: `deploy_stackset.py --stage auto` — every guardrail with `require_approval: false` (default) deploys immediately to its declared target.
5. **ApproveDeploy** — manual approval gate (SNS), only gates guardrails flagged `require_approval: true`.
6. **DeployApproved**: `deploy_stackset.py --stage approved` — deploys only those flagged guardrails.
7. In each target account: a local `AWS::Bedrock::Guardrail`, an immutable `AWS::Bedrock::GuardrailVersion` (workloads consume the version, never `DRAFT`), and SSM Parameters at `/security/bedrock/guardrail/<nombre>/id|version`.

Key design points to preserve when editing:
- StackSets use `PermissionModel: SERVICE_MANAGED` + `CallAs: DELEGATED_ADMIN`.
- One StackSet **per guardrail**, single target — don't reintroduce per-environment StackSet suffixes or collapse multiple guardrails into one StackSet.
- `require_approval` is the only thing that splits guardrails across pipeline stages — don't reintroduce env-based branching.
- `scripts/deploy_stackset.py` must remain idempotent and must fail the CodeBuild step on any StackSet operation failure — don't swallow errors, and don't add a manual guardrail registry (auto-discovery is the point).
- Mandatory tags on deployed resources: `ManagedBy: secdevops-stacksets`, `Environment` (free-form via `EnvironmentTag`, purely descriptive).
- IAM roles are split by function (pipeline / validate / deploy) with least privilege.
- Artifact bucket is encrypted (KMS), versioned, blocks public access, denies insecure transport, 90-day lifecycle, and has `LoggingConfiguration` pointing at a dedicated `AccessLogsBucket` (versioned, AES256, own 90-day lifecycle) — preserve when modifying `pipeline/pipeline.yaml`. `ApprovalTopic` (SNS) uses `KmsMasterKeyId: alias/aws/sns`. `DeployBuildRole`'s `StackSetsDelegatedAdmin` statement has a documented `checkov:skip` for `CKV_AWS_111` (StackSets create/update actions don't support resource-level ARNs) — keep that Metadata block if touching the role.

## Resolved issue: pipeline wasn't auto-triggering on merge

Observed 2026-08-17 through 2026-08-19: merges to `main` never fired the CodePipeline execution automatically (`aws codepipeline list-pipeline-executions` only showed manual `StartPipelineExecution` triggers, never `Webhook`), even though the CodeConnections `Connection` showed `ConnectionStatus: AVAILABLE` and `pipeline.yaml`'s source action had `DetectChanges: true`. `ConnectionStatus: AVAILABLE` only confirms the account-level handshake — it does NOT mean the underlying GitHub App ("AWS Connector for GitHub") has been granted access to this specific repo.

Root cause (found 2026-08-19 by diffing against a working CodeConnections-sourced pipeline in another account, which had `Trigger: Webhook` executions): the "AWS Connector for GitHub" App installation was scoped to "Only select repositories" and `bedrock-guardrails-stacksets` was not in that list, so GitHub never sent push webhooks for it. Fixed by adding the repo to the App's repository access list at `https://github.com/settings/installations` (or the org equivalent) → AWS Connector for GitHub → Configure.

If this resurfaces on a new repo/connection: check the GitHub App's repository access list first, before assuming a CloudFormation/`Triggers` config issue.

## Working with this repo

- No package manager / build step. Python dependency is `boto3`; lint/scan tools are `cfn-lint`, `checkov`.
- Validate locally before every commit:
  ```
  cfn-lint guardrails/*/template.yaml pipeline/pipeline.yaml bootstrap/*.yaml
  checkov -d guardrails --framework cloudformation --compact
  checkov -d pipeline --framework cloudformation --compact
  ```
- To add a new guardrail: copy an existing folder (e.g. `guardrails/general/`), change `Name`/`GuardrailName` in the template, and set the target (`organizational_unit_ids`/`account_ids`) + `require_approval` in `targets.json`. No other file needs to change — discovery is automatic. A guardrail needing multiple distinct targets = multiple guardrail directories, not multiple entries in one `targets.json`.
- `scripts/deploy_stackset.py` can be run manually for debugging: `python scripts/deploy_stackset.py --stage auto` (requires AWS credentials for the delegated-admin/SecDevOps account; discovers `guardrails/` relative to cwd, override with `--guardrails-dir`).
- This is a Spanish-language codebase (comments, README, commit messages) — match that when writing comments or docs in these files. Commit messages: Spanish, Conventional Commits (`feat:`, `fix:`, `docs:`, `ci:`).

## Git workflow — important

- **Never commit or push directly to `main`** in this repo — the org blocks direct pushes. Every change goes through a PR and must pass the required check **"CencoCorp Scan Security"** (an org-level workflow, not defined in this repo).
- Always create a branch first: `git checkout -b feat/...` (or `fix/...`, `docs/...`, `ci/...`) before making any commits here.
- **Don't merge a PR mid-session while more commits are still coming for it** — a commit pushed to a branch after its PR is already merged becomes orphaned (never reaches `main`). This happened once: `bootstrap/github-oidc-role.yaml` was pushed to `feat/multi-guardrail-bootstrap-secdevops` after PR #1 had already been merged, and had to be recovered via `git cherry-pick` into a later branch.
