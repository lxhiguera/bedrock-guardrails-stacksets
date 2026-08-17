# Bootstrap: delegated admin de StackSets para SecDevOps

Esto **no se puede hacer con CloudFormation** (son acciones a nivel de
Organizations que solo puede ejecutar la cuenta management). Ejecutar estos
comandos AWS CLI **una sola vez**, autenticado en la cuenta management
(`758626604929`).

Valores de esta Landing Zone:

- Cuenta management (Organizations): `758626604929`
- Cuenta SecDevOps (delegated admin): `474632925684`
- OU DEV: `ou-p9fr-dqd5qn16`
- OU SECDEVOPS: `ou-p9fr-f0v77vbs`

## 1. Habilitar trusted access de StackSets con Organizations

```bash
aws organizations enable-aws-service-access \
  --service-principal member.org.stacksets.cloudformation.amazonaws.com
```

## 2. Registrar SecDevOps como delegated administrator

```bash
aws organizations register-delegated-administrator \
  --account-id 474632925684 \
  --service-principal member.org.stacksets.cloudformation.amazonaws.com
```

## 3. Verificar

```bash
aws organizations list-delegated-administrators \
  --service-principal member.org.stacksets.cloudformation.amazonaws.com
```

Debe listar la cuenta `474632925684`.

Con esto, desde la cuenta SecDevOps se pueden crear StackSets
`SERVICE_MANAGED` pasando `CallAs=DELEGATED_ADMIN` (que es justo lo que hace
`scripts/deploy_stackset.py`), sin necesitar credenciales de la cuenta
management para cada despliegue.

## Nota sobre las OUs de destino

Esta Landing Zone hoy solo tiene OU `DEV` (`ou-p9fr-dqd5qn16`) y OU
`SECDEVOPS` (`ou-p9fr-f0v77vbs`, donde vive la cuenta del pipeline). No
existen todavía OUs de QA ni Prod. `guardrails/config/deployment-targets.json`
apunta `qa` y `prod` temporalmente a la OU DEV (ver los campos `_todo` en ese
archivo) — actualízalos cuando crees las OUs reales en Control Tower.
