# Bedrock Guardrails centralizados via StackSets

Despliegue centralizado de **Bedrock Guardrails locales** (recurso `
AWS::Bedrock::Guardrail` creado en cada cuenta/región) desde la cuenta **SecDevOps**
, usando **CloudFormation StackSets** con modelo **SERVICE_MANAGED** y **
administrador delegado**.

> No son *organizational guardrails* (políticas a nivel organización): cada
> cuenta recibe su propio guardrail local, pero la definición, versión y ciclo de
> vida se gobiernan centralmente desde SecDevOps.

## Arquitectura

```
┌─────────────┐   push/merge a main    ┌──────────────────────────────────────────────┐
│   GitHub    │───────────────────────▶│  Cuenta SecDevOps (delegated admin StackSets)│
│  (repo IaC) │                        │                                              │
│             │   PRs: GitHub Actions  │  CodePipeline (V2)                           │
│  Actions:   │   (cfn-lint, checkov)  │   1. Source  ── CodeConnections (GitHub)     │
│  pr-validate│                        │   2. Validate ─ CodeBuild (lint + checkov)   │
└─────────────┘                        │   3. Deploy   ─ CodeBuild → guardrails       │
                                       │      (require_approval=false)                │
                                       │   4. ApproveDeploy ─ Manual (SNS)             │
                                       │   5. DeployApproved ─ CodeBuild → guardrails  │
                                       │      (require_approval=true)                 │
                                       └──────────────────┬───────────────────────────┘
                                                          │ StackSets SERVICE_MANAGED
                                                          │ (1 StackSet por guardrail)
                          ┌───────────────────────────────┼───────────────────────────────┐
                          ▼                               ▼                               ▼
                 ┌─────────────────┐            ┌─────────────────┐            ┌─────────────────┐
                 │  Guardrail A    │            │  Guardrail B    │            │  Guardrail C    │
                 │  → OU/cuenta X  │            │  → OU/cuenta Y  │            │  → OU/cuenta Z  │
                 │  Stack instance:│            │  Stack instance:│            │  Stack instance:│
                 │  · Guardrail    │            │  · Guardrail    │            │  · Guardrail    │
                 │  · GuardrailVer │            │  · GuardrailVer │            │  · GuardrailVer │
                 │  · SSM params   │            │  · SSM params   │            │  · SSM params   │
                 └─────────────────┘            └─────────────────┘            └─────────────────┘
```

Cada guardrail declara **su propio destino** (OU o cuenta específica, la que
sea) en su `targets.json` — no existe el concepto de "entornos" dev/qa/prod
como destino. Lo único que varía por guardrail es si necesita aprobación
manual antes de aplicarse (`require_approval`).

### Flujo

1.  **PR en GitHub** → GitHub Actions ejecuta `cfn-lint`, `checkov` y validación
    de JSON (shift-left, sin tocar AWS o con rol OIDC de solo lectura).
2.  **Merge a `main`** → CodeConnections dispara **CodePipeline** en SecDevOps.
3.  **Validate** repite lint + scan dentro de AWS (no confiar solo en checks del
    lado de GitHub).
4.  **Deploy**: CodeBuild ejecuta `scripts/deploy_stackset.py --stage auto`, que
    **descubre automáticamente** cada carpeta en `guardrails/` y despliega de
    inmediato cualquier guardrail con `require_approval: false` (o ausente) —
    crea/actualiza su StackSet (`SERVICE_MANAGED`, `CallAs=DELEGATED_ADMIN`) y
    sus stack instances por OU/cuenta/región definidos en su `targets.json`.
5.  **ApproveDeploy** — gate de aprobación manual (SNS) antes de aplicar los
    guardrails marcados `require_approval: true`.
6.  **DeployApproved**: mismo script con `--stage approved`, despliega solo
    esos guardrails.
7.  En cada cuenta destino se crea el guardrail **local**, una **versión
    inmutable publicada** y **SSM Parameters** con namespace por guardrail
    (`/security/bedrock/guardrail/<nombre>/id|version`) para que los
    equipos de aplicación referencien el guardrail sin hardcodear IDs.

## Estructura del repo

```
├── guardrails/
│   └── <nombre>/                      # Un directorio por guardrail (ej. general/)
│       ├── template.yaml              # AWS::Bedrock::Guardrail + Version + SSM
│       └── targets.json               # Un destino (OU/cuentas), regiones, parámetros, require_approval
├── pipeline/pipeline.yaml             # Bootstrap del pipeline en SecDevOps
├── bootstrap/
│   ├── delegated-admin.md             # Comandos CLI (cuenta management, una vez)
│   └── codeconnections.yaml           # Conexión GitHub en cuenta SecDevOps
├── buildspecs/
│   ├── validate.yaml
│   └── deploy.yaml
├── scripts/deploy_stackset.py         # Despliegue idempotente multi-guardrail
└── .github/workflows/pr-validate.yml  # Validación en PRs
```

### Modelo multi-guardrail

`scripts/deploy_stackset.py` no tiene un registro manual de guardrails: recorre
`guardrails/*/` y toma cualquier carpeta que tenga `template.yaml` +
`targets.json`. Reglas:

- **Un StackSet por guardrail** (`stackset_name` en su `targets.json`) — un
  único destino, no hay sufijos de entorno.
- `targets.json` siempre lleva `organizational_unit_ids`; opcionalmente
  `account_ids` + `account_filter_type` (`INTERSECTION` o `DIFFERENCE`) para
  acotar a cuentas específicas dentro de una OU. Puede ser cualquier OU o
  cuenta — no está atado a un concepto de "entorno".
- `require_approval` (`true`/`false`, default `false`) decide en qué etapa del
  pipeline cae ese guardrail: `Deploy` (inmediato) o `DeployApproved` (después
  del gate manual `ApproveDeploy`).
- **Guardrail nuevo**: copiar una carpeta existente (ej. `guardrails/general/`),
  cambiar el `Name`/`GuardrailName` en el template y ajustar `targets.json`
  (destino, `require_approval`, parámetros). No hace falta registrar nada más
  — el script lo detecta solo.
- Si un guardrail necesita aplicarse a **varios destinos distintos** con
  configuraciones diferentes, se crean carpetas separadas (ej.
  `guardrails/general-sandbox/`, `guardrails/general-clientes/`), cada una con
  su propio `targets.json` — no un solo guardrail con múltiples destinos.

Ejemplo de `targets.json`:
```json
{
  "stackset_name": "bedrock-guardrail-general",
  "call_as": "DELEGATED_ADMIN",
  "permission_model": "SERVICE_MANAGED",
  "require_approval": false,
  "auto_deployment": { "Enabled": true, "RetainStacksOnAccountRemoval": false },
  "operation_preferences": { "FailureTolerancePercentage": 0, "MaxConcurrentPercentage": 100 },
  "organizational_unit_ids": ["ou-p9fr-dqd5qn16"],
  "regions": ["us-east-1"],
  "parameters": { "GuardrailName": "general", "EnvironmentTag": "shared", "...": "..." }
}
```

### Validación local antes de commitear

```bash
cfn-lint guardrails/*/template.yaml pipeline/pipeline.yaml
checkov -d guardrails --framework cloudformation --compact
```
## Requisitos previos (bootstrap, una sola vez)

Esta Landing Zone concreta usa: cuenta SecDevOps `474632925684`, cuenta
management `758626604929`, OU DEV `ou-p9fr-dqd5qn16` (único destino usado
hoy — el guardrail `general` apunta ahí porque es la única OU con cuentas de
trabajo en esta Landing Zone; no representa una etapa "dev" del pipeline).

1.  **Delegated admin**: desde la cuenta management, registrar SecDevOps como
    administrador delegado de CloudFormation StackSets y habilitar *trusted
    access* con Organizations. No se puede hacer con CloudFormation — seguir
    los comandos AWS CLI en [`bootstrap/delegated-admin.md`](bootstrap/delegated-admin.md).
2.  **Conexión GitHub**: crear una conexión **CodeConnections** hacia GitHub
    en la cuenta SecDevOps (no en la cuenta management) con
    [`bootstrap/codeconnections.yaml`](bootstrap/codeconnections.yaml):
    ```bash
    aws cloudformation deploy \
      --template-file bootstrap/codeconnections.yaml \
      --stack-name bedrock-guardrails-github-connection \
      --region us-east-1
    ```
    Luego aprobarla manualmente en la consola (CodePipeline > Settings >
    Connections) — queda `PENDING` hasta autorizar el acceso a GitHub. Copiar
    el `ConnectionArn` de los outputs del stack para el paso siguiente.
3.  **Desplegar el pipeline**:
    ```bash
    aws cloudformation deploy \
      --template-file pipeline/pipeline.yaml \
      --stack-name bedrock-guardrails-pipeline \
      --capabilities CAPABILITY_NAMED_IAM \
      --region us-east-1 \
      --parameter-overrides \
        GitHubConnectionArn=<arn del paso 2> \
        GitHubRepo=<owner/repo>
    ```
    en la cuenta SecDevOps (`474632925684`).
4.  **OIDC para GitHub Actions** (opcional — habilita el job `aws-validate`
    de `pr-validate.yml`): crear el rol `github-actions-validate-role` con
    [`bootstrap/github-oidc-role.yaml`](bootstrap/github-oidc-role.yaml) en
    la cuenta SecDevOps:
    ```bash
    aws cloudformation deploy \
      --template-file bootstrap/github-oidc-role.yaml \
      --stack-name bedrock-guardrails-github-oidc \
      --capabilities CAPABILITY_NAMED_IAM \
      --region us-east-1 \
      --parameter-overrides \
        GitHubOrg=lxhiguera \
        GitHubRepoName=bedrock-guardrails-stacksets
    ```
    Si esta cuenta **ya tiene** un proveedor OIDC de GitHub configurado (por
    otro pipeline, `token.actions.githubusercontent.com`), agregar
    `CreateOidcProvider=false` — AWS solo permite un proveedor OIDC por URL
    por cuenta y el deploy fallaría al intentar crear un duplicado. El rol
    creado solo tiene permiso `cloudformation:ValidateTemplate` (solo
    lectura) y su trust policy limita quién puede asumirlo a
    `repo:lxhiguera/bedrock-guardrails-stacksets:*` vía OIDC — sin llaves
    estáticas.
5.  `guardrails/general/targets.json` ya está editado con los valores reales
    de esta Landing Zone.

## Flujo de trabajo en git

- **No se permite push directo a `main`.** Todo cambio va por Pull Request y
  debe pasar el required check **"CencoCorp Scan Security"** (workflow de la
  organización, fuera de este repo).
- Antes de commitear, crear siempre una rama: `git checkout -b feat/...` (o
  `fix/...`, `docs/...`, `ci/...`).
- Mensajes de commit en español, estilo *Conventional Commits*
  (`feat:`, `fix:`, `docs:`, `ci:`).

## Mejores prácticas incorporadas

**Gobernanza y despliegue**

- `SERVICE_MANAGED` \+ delegated admin: sin roles `AWSCloudFormationStackSet*`
  manuales por cuenta y **auto-deployment** a cuentas nuevas que entren a la OU.
- Targets por **OU**, nunca listas de cuentas hardcodeadas.
- `FailureTolerancePercentage` y `MaxConcurrentPercentage` configurables para
  despliegues progresivos y contención de fallos.
- Un StackSet por guardrail, con un destino explícito (OU/cuenta) definido en
  su propio `targets.json` — no atado a un concepto fijo de entornos.
- Aprobación manual opcional **por guardrail** (`require_approval`), no un
  gate global de "producción".

**Guardrail**

- `AWS::Bedrock::GuardrailVersion`: los workloads consumen una versión
  inmutable, no `DRAFT`. Un cambio no aprobado en el draft no afecta producción.
- Publicación de ID/versión en **SSM Parameter Store** con path estándar por
  guardrail: los equipos hacen
  `{{resolve:ssm:/security/bedrock/guardrail/general/id}}` y quedan
  desacoplados.
- Cobertura completa: filtros de contenido (incl. `PROMPT_ATTACK`), temas
  denegados, PII (bloqueo de credenciales AWS, tarjetas; anonimización de
  email/teléfono), listas de palabras y *contextual grounding* anti-alucinación.
- Soporte opcional de **KMS CMK** por parámetro.
- Tag `ManagedBy: secdevops-stacksets` para identificar recursos gestionados y
  detectar drift.

**Pipeline y seguridad**

- Fuente GitHub vía **CodeConnections** (nada de tokens/webhooks legacy).
- GitHub Actions con **OIDC** (sin llaves estáticas en secrets) y solo permisos
  de validación; el despliegue vive exclusivamente en AWS.
- Doble validación: en PR (GitHub) y dentro del pipeline (no confiar en checks
  externos).
- Roles IAM separados por función (pipeline / validate / deploy) con mínimo
  privilegio.
- Bucket de artefactos cifrado (KMS), versionado, sin acceso público, TLS
  obligatorio y lifecycle de 90 días.
- Script de despliegue **idempotente**: create-or-update, espera de operaciones y
  fallo explícito del build si la operación del StackSet falla.

**Recomendaciones operativas adicionales**

- Habilitar **drift detection** periódica del StackSet (EventBridge Scheduler →
  Lambda → `DetectStackSetDrift`) y alertar si alguna cuenta modificó su
  guardrail localmente.
- Proteger los guardrails en cuentas miembro con una **SCP** que deniegue `
  bedrock:DeleteGuardrail` y `bedrock:UpdateGuardrail` salvo al rol de ejecución
  de StackSets.
- Monitorear invocaciones bloqueadas con CloudWatch (métricas de Bedrock) y
  centralizar logs de invocación si se requiere auditoría.
- Versionar cambios de política del guardrail vía PR review obligatorio
  (CODEOWNERS → equipo de seguridad).
