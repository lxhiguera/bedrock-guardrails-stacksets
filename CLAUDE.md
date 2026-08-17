# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

CI/CD pipeline (CloudFormation + CodePipeline + CodeBuild) that centrally deploys **local AWS Bedrock Guardrails** (the `AWS::Bedrock::Guardrail` resource, created in each target account — NOT organizational guardrails) to every AWS account in an organization via CloudFormation **StackSets** (`SERVICE_MANAGED`, `CallAs=DELEGATED_ADMIN`), governed from a central "SecDevOps" account. This is infrastructure-as-code only — there is no application code, build system, or test framework; "testing" means template linting/security scanning (`cfn-lint`, `checkov`).

**Multi-guardrail model**: this repo can manage several distinct guardrails at once, each with its own lifecycle, targets, and parameters — not just one.

## Landing Zone (this deployment)

- SecDevOps account (pipeline + StackSets delegated admin): `474632925684`
- Organizations management account: `758626604929`
- OU DEV: `ou-p9fr-dqd5qn16` — the only workload OU that exists today
- OU SECDEVOPS: `ou-p9fr-f0v77vbs`
- No QA/Prod OUs exist yet — each guardrail's `targets.json` points `qa`/`prod` at the DEV OU as a stopgap (see `_todo` keys); update when those OUs are created in Control Tower.
- Region: `us-east-1` for all environments.

## Repo layout

- `guardrails/<nombre>/` — **one directory per guardrail** (e.g. `guardrails/general/`). No manual registry anywhere — `scripts/deploy_stackset.py` auto-discovers any subdirectory of `guardrails/` containing both files below.
  - `template.yaml` — `AWS::Bedrock::Guardrail` + `AWS::Bedrock::GuardrailVersion` + SSM Parameters (`/security/bedrock/guardrail/<nombre>/<env>/id|version` — the guardrail name is part of the SSM path, never break this shape). Takes a `GuardrailName` parameter that must match the directory name.
  - `targets.json` — per-environment (`dev`/`qa`/`prod`) config: `stackset_prefix` (StackSet = `<stackset_prefix>-<env>`), `organizational_unit_ids` (always), optional `account_ids` + `account_filter_type` (`INTERSECTION`/`DIFFERENCE`) to narrow to specific accounts within an OU, `regions`, and template `parameters`. **If an environment key is absent, that guardrail is silently skipped for that pipeline stage — this is expected, not a bug.**
- `pipeline/pipeline.yaml` — bootstrap CloudFormation template for the pipeline itself (S3 artifact bucket, IAM roles, CodeBuild projects, CodePipeline: Source → Validate → DeployDev → DeployQA → ApproveProd (manual/SNS) → DeployProd). Deployed **once, manually** into the SecDevOps account.
- `bootstrap/delegated-admin.md` — AWS CLI commands (not CloudFormation — Organizations-level action) to run once in the management account to register SecDevOps as StackSets delegated admin and enable trusted access.
- `bootstrap/codeconnections.yaml` — creates the `AWS::CodeStarConnections::Connection` to GitHub in the SecDevOps account. Must be deployed before `pipeline/pipeline.yaml`; approve manually in console (PENDING → AVAILABLE), then pass its ARN as `GitHubConnectionArn`. The connection must live in the **same account as the pipeline** (SecDevOps) — `PipelineRole` in `pipeline/pipeline.yaml` only grants `codestar-connections:UseConnection` within that account.
- `buildspecs/validate.yaml` — CodeBuild spec: `cfn-lint guardrails/*/template.yaml pipeline/pipeline.yaml`, `checkov -d guardrails`/`-d pipeline`, loops `aws cloudformation validate-template` and JSON validation over every `guardrails/*/template.yaml` / `guardrails/*/targets.json`.
- `buildspecs/deploy.yaml` — CodeBuild spec: runs `scripts/deploy_stackset.py --env "${TARGET_ENV}"` (no `--config`/`--template` args — auto-discovery), `TARGET_ENV` injected per pipeline stage via `EnvironmentVariables` in `pipeline/pipeline.yaml`.
- `scripts/deploy_stackset.py` — idempotent, multi-guardrail: discovers `guardrails/*/`, and for each guardrail whose `targets.json` defines the requested `--env`, creates/updates its StackSet and stack instances, polling `describe_stack_set_operation` until `SUCCEEDED`/`FAILED`/`STOPPED` and hard-failing the build on failure/stop.
- `.github/workflows/pr-validate.yml` — PR-time shift-left checks only (lint/checkov/JSON validation via GitHub Actions, glob over `guardrails/*/`), plus an optional `cloudformation validate-template` job via GitHub OIDC against a read-only IAM role in SecDevOps (`github-actions-validate-role`). **Deployment must never move to GitHub Actions** — it lives exclusively in AWS CodePipeline.

## Architecture / flow

1. PR opened → GitHub Actions (`pr-validate.yml`) runs `cfn-lint` + `checkov` + JSON validation (OIDC, read-only).
2. Merge to `main` → CodeConnections triggers CodePipeline in SecDevOps.
3. **Validate** stage repeats lint/scan inside AWS (pipeline does not trust GitHub-side checks alone).
4. **DeployDev** / **DeployQA** stages run `deploy_stackset.py --env <env>`, looping over every guardrail directory that defines that environment.
5. **ApproveProd** — manual approval gate (SNS) before anything reaches prod.
6. **DeployProd** — same script/pattern, targeting each guardrail's prod StackSet.
7. In each target account: a local `AWS::Bedrock::Guardrail`, an immutable `AWS::Bedrock::GuardrailVersion` (workloads consume the version, never `DRAFT`), and SSM Parameters at `/security/bedrock/guardrail/<nombre>/<env>/id|version`.

Key design points to preserve when editing:
- StackSets use `PermissionModel: SERVICE_MANAGED` + `CallAs: DELEGATED_ADMIN` — no per-account `AWSCloudFormationStackSet*` IAM roles, auto-deployment covers new accounts joining an OU.
- One StackSet **per guardrail per environment** — don't collapse multiple guardrails into one StackSet.
- `scripts/deploy_stackset.py` must remain idempotent and must fail the CodeBuild step on any StackSet operation failure — don't swallow errors, and don't add a manual guardrail registry (auto-discovery is the point).
- Mandatory tags on deployed resources: `ManagedBy: secdevops-stacksets`, `Environment`.
- IAM roles are split by function (pipeline / validate / deploy) with least privilege; keep that separation.
- Artifact bucket is encrypted (KMS), versioned, blocks public access, denies insecure transport, 90-day lifecycle — preserve when modifying `pipeline/pipeline.yaml`.

## Working with this repo

- No package manager / build step. Python dependency is `boto3`; lint/scan tools are `cfn-lint`, `checkov`.
- Validate locally before every commit:
  ```
  cfn-lint guardrails/*/template.yaml pipeline/pipeline.yaml
  checkov -d guardrails --framework cloudformation --compact
  ```
- To add a new guardrail: copy an existing folder (e.g. `guardrails/general/`), change `Name`/`GuardrailName` in the template and adjust `targets.json`. No other file needs to change — discovery is automatic.
- `scripts/deploy_stackset.py` can be run manually for debugging: `python scripts/deploy_stackset.py --env dev` (requires AWS credentials for the delegated-admin/SecDevOps account; discovers `guardrails/` relative to cwd, override with `--guardrails-dir`).
- This is a Spanish-language codebase (comments, README, commit messages) — match that when writing comments or docs in these files. Commit messages: Spanish, Conventional Commits (`feat:`, `fix:`, `docs:`, `ci:`).

## Git workflow — important

- **Never commit or push directly to `main`** in this repo — the org blocks direct pushes. Every change goes through a PR and must pass the required check **"CencoCorp Scan Security"** (an org-level workflow, not defined in this repo).
- Always create a branch first: `git checkout -b feat/...` (or `fix/...`, `docs/...`, `ci/...`) before making any commits here.
