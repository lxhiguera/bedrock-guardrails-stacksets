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
└─────────────┘                        │   3. DeployDev ─ CodeBuild → StackSet dev    │
                                       │   4. DeployQA  ─ CodeBuild → StackSet qa     │
                                       │   5. Approval  ─ Manual (SNS)                │
                                       │   6. DeployProd─ CodeBuild → StackSet prod   │
                                       └──────────────────┬───────────────────────────┘
                                                          │ StackSets SERVICE_MANAGED
                                                          │ (targets = OUs, auto-deployment)
                          ┌───────────────────────────────┼───────────────────────────────┐
                          ▼                               ▼                               ▼
                 ┌─────────────────┐            ┌─────────────────┐            ┌─────────────────┐
                 │  OU Dev         │            │  OU QA          │            │  OU Prod        │
                 │  Cuenta A, B... │            │  Cuenta C...    │            │  Cuenta D, E... │
                 │  Stack instance:│            │  Stack instance:│            │  Stack instance:│
                 │  · Guardrail    │            │  · Guardrail    │            │  · Guardrail    │
                 │  · GuardrailVer │            │  · GuardrailVer │            │  · GuardrailVer │
                 │  · SSM params   │            │  · SSM params   │            │  · SSM params   │
                 └─────────────────┘            └─────────────────┘            └─────────────────┘
```
### Flujo

1.  **PR en GitHub** → GitHub Actions ejecuta `cfn-lint`, `checkov` y validación
    de JSON (shift-left, sin tocar AWS o con rol OIDC de solo lectura).
2.  **Merge a `main`** → CodeConnections dispara **CodePipeline** en SecDevOps.
3.  **Validate** repite lint + scan dentro de AWS (no confiar solo en checks del
    lado de GitHub).
4.  **Deploy** por entorno: CodeBuild ejecuta `scripts/deploy_stackset.py`, que
    crea/actualiza el StackSet (`SERVICE_MANAGED`, `CallAs=DELEGATED_ADMIN`) y
    sus stack instances por OU/región.
5.  **Aprobación manual** antes de prod (SNS notifica a los aprobadores).
6.  En cada cuenta destino se crea el guardrail **local**, una **versión
    inmutable publicada** y **SSM Parameters** (`
    /security/bedrock/guardrail/\<env>/id|version`) para que los equipos de
    aplicación referencien el guardrail sin hardcodear IDs.

## Estructura del repo

```
├── guardrails/
│   ├── template.yaml                  # AWS::Bedrock::Guardrail + Version + SSM
│   └── config/deployment-targets.json # OUs, regiones, parámetros por entorno
├── pipeline/pipeline.yaml             # Bootstrap del pipeline en SecDevOps
├── buildspecs/
│   ├── validate.yaml
│   └── deploy.yaml
├── scripts/deploy_stackset.py         # Despliegue idempotente del StackSet
└── .github/workflows/pr-validate.yml  # Validación en PRs
```
## Requisitos previos (bootstrap, una sola vez)

1.  **Delegated admin**: desde la cuenta de management de la organización,
    registrar la cuenta SecDevOps como administrador delegado de CloudFormation
    StackSets y habilitar *trusted access* con Organizations.
2.  **Conexión GitHub**: crear una conexión **CodeConnections** hacia GitHub en
    la cuenta SecDevOps y aprobarla (queda `PENDING` hasta autorizarla en la
    consola).
3.  **Desplegar el pipeline**: `aws cloudformation deploy --template-file
    pipeline/pipeline.yaml --stack-name bedrock-guardrails-pipeline
    \--capabilities CAPABILITY_NAMED_IAM --parameter-overrides
    GitHubConnectionArn=\<arn> GitHubRepo=\<owner/repo>` en la cuenta SecDevOps.
4.  **OIDC para GitHub Actions** (opcional): crear el rol `
    github-actions-validate-role` con trust hacia el proveedor OIDC de GitHub,
    permisos de solo lectura (`cloudformation:ValidateTemplate`).
5.  Editar `guardrails/config/deployment-targets.json` con tus OUs y regiones
    reales.

## Mejores prácticas incorporadas

**Gobernanza y despliegue**

- `SERVICE_MANAGED` \+ delegated admin: sin roles `AWSCloudFormationStackSet*`
  manuales por cuenta y **auto-deployment** a cuentas nuevas que entren a la OU.
- Targets por **OU**, nunca listas de cuentas hardcodeadas.
- `FailureTolerancePercentage` y `MaxConcurrentPercentage` configurables para
  despliegues progresivos y contención de fallos.
- Un StackSet por entorno (`\-dev`, `\-qa`, `\-prod`) con parámetros
  diferenciados (dev con filtros MEDIUM, prod HIGH).
- Aprobación manual antes de producción.

**Guardrail**

- `AWS::Bedrock::GuardrailVersion`: los workloads consumen una versión
  inmutable, no `DRAFT`. Un cambio no aprobado en el draft no afecta producción.
- Publicación de ID/versión en **SSM Parameter Store** con path estándar: los
  equipos hacen `{{resolve:ssm:/security/bedrock/guardrail/prod/id}}` y quedan
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
