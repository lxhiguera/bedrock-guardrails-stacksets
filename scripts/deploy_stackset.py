#!/usr/bin/env python3
"""
Despliegue idempotente multi-guardrail de StackSets SERVICE_MANAGED
(delegated admin) para Bedrock Guardrails locales.

Descubre automaticamente cada guardrails/<nombre>/ que contenga
template.yaml + targets.json. Por cada guardrail que defina el entorno
solicitado en targets.json:
- Crea el StackSet si no existe; lo actualiza si existe.
- Crea/actualiza stack instances apuntando a las OUs (y opcionalmente
  cuentas especificas dentro de ellas) configuradas.
- Espera a que las operaciones terminen y falla el build si alguna falla.

Un guardrail que no define el entorno solicitado en su targets.json se
omite silenciosamente para esa etapa (no es un error).
"""
import argparse
import json
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

POLL_SECONDS = 20


def wait_for_operation(cfn, stackset_name, operation_id, call_as):
    while True:
        op = cfn.describe_stack_set_operation(
            StackSetName=stackset_name,
            OperationId=operation_id,
            CallAs=call_as,
        )["StackSetOperation"]
        status = op["Status"]
        print(f"  Operacion {operation_id}: {status}")
        if status in ("SUCCEEDED",):
            return
        if status in ("FAILED", "STOPPED"):
            reason = op.get("StatusReason", "sin detalle")
            sys.exit(f"ERROR: operacion {status}: {reason}")
        time.sleep(POLL_SECONDS)


def build_deployment_targets(env_cfg):
    targets = {"OrganizationalUnitIds": env_cfg["organizational_unit_ids"]}
    if env_cfg.get("account_ids"):
        targets["Accounts"] = env_cfg["account_ids"]
        targets["AccountFilterType"] = env_cfg.get("account_filter_type", "INTERSECTION")
    return targets


def discover_guardrails(guardrails_dir):
    guardrails = []
    for path in sorted(Path(guardrails_dir).iterdir()):
        if not path.is_dir():
            continue
        template_path = path / "template.yaml"
        targets_path = path / "targets.json"
        if template_path.exists() and targets_path.exists():
            guardrails.append((path.name, template_path, targets_path))
    return guardrails


def deploy_guardrail(cfn, name, template_path, targets_path, env):
    with open(targets_path) as f:
        cfg = json.load(f)

    if env not in cfg.get("environments", {}):
        print(f"[{name}] entorno '{env}' no definido en targets.json, se omite.")
        return

    env_cfg = cfg["environments"][env]
    stackset_name = f'{cfg["stackset_prefix"]}-{env}'
    call_as = cfg.get("call_as", "DELEGATED_ADMIN")
    op_prefs = cfg.get("operation_preferences", {})
    deployment_targets = build_deployment_targets(env_cfg)

    with open(template_path) as f:
        template_body = f.read()

    parameters = [
        {"ParameterKey": k, "ParameterValue": v}
        for k, v in env_cfg["parameters"].items()
    ]

    print(f"[{name}] Desplegando StackSet {stackset_name} para entorno {env}...")

    common = dict(StackSetName=stackset_name, CallAs=call_as)

    try:
        cfn.describe_stack_set(**common)
        exists = True
    except ClientError as e:
        if e.response["Error"]["Code"] == "StackSetNotFoundException":
            exists = False
        else:
            raise

    stackset_args = dict(
        common,
        TemplateBody=template_body,
        Parameters=parameters,
        Capabilities=["CAPABILITY_NAMED_IAM"],
        PermissionModel=cfg.get("permission_model", "SERVICE_MANAGED"),
        AutoDeployment=cfg.get(
            "auto_deployment",
            {"Enabled": True, "RetainStacksOnAccountRemoval": False},
        ),
        Tags=[
            {"Key": "ManagedBy", "Value": "secdevops-stacksets"},
            {"Key": "Environment", "Value": env},
        ],
    )

    if not exists:
        print(f"[{name}] Creando StackSet {stackset_name}...")
        cfn.create_stack_set(**stackset_args)
    else:
        print(f"[{name}] Actualizando StackSet {stackset_name}...")
        try:
            op = cfn.update_stack_set(
                **stackset_args,
                OperationPreferences=op_prefs,
                DeploymentTargets=deployment_targets,
                Regions=env_cfg["regions"],
            )
            wait_for_operation(cfn, stackset_name, op["OperationId"], call_as)
        except ClientError as e:
            if "No updates" in str(e):
                print(f"[{name}] Sin cambios en el template/parametros.")
            else:
                raise

    instances = cfn.list_stack_instances(**common).get("Summaries", [])
    if not instances:
        print(f"[{name}] Creando stack instances...")
        op = cfn.create_stack_instances(
            **common,
            DeploymentTargets=deployment_targets,
            Regions=env_cfg["regions"],
            OperationPreferences=op_prefs,
        )
        wait_for_operation(cfn, stackset_name, op["OperationId"], call_as)
    else:
        print(f"[{name}] {len(instances)} stack instances existentes.")

    for inst in cfn.list_stack_instances(**common).get("Summaries", []):
        print(
            f'  [{name}] {inst["Account"]} / {inst["Region"]}: '
            f'{inst.get("StackInstanceStatus", {}).get("DetailedStatus", inst.get("Status"))}'
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, choices=["dev", "qa", "prod"])
    parser.add_argument("--guardrails-dir", default="guardrails")
    args = parser.parse_args()

    guardrails = discover_guardrails(args.guardrails_dir)
    if not guardrails:
        sys.exit(f"ERROR: no se encontraron guardrails en {args.guardrails_dir}/")

    print(f"Guardrails detectados: {', '.join(name for name, _, _ in guardrails)}")

    cfn = boto3.client("cloudformation")
    for name, template_path, targets_path in guardrails:
        deploy_guardrail(cfn, name, template_path, targets_path, args.env)

    print("Despliegue completado.")


if __name__ == "__main__":
    main()
