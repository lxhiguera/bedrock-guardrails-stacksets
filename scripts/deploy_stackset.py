#!/usr/bin/env python3
"""
Despliegue idempotente de un StackSet SERVICE_MANAGED (delegated admin)
para Bedrock Guardrails locales.

- Crea el StackSet si no existe; lo actualiza si existe.
- Crea/actualiza stack instances apuntando a las OUs configuradas.
- Espera a que las operaciones terminen y falla el build si la operacion falla.
"""
import argparse
import json
import sys
import time

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, choices=["dev", "qa", "prod"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--template", required=True)
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    with open(args.template) as f:
        template_body = f.read()

    env_cfg = cfg["environments"][args.env]
    stackset_name = f'{cfg["stackset_name"]}-{args.env}'
    call_as = cfg.get("call_as", "DELEGATED_ADMIN")
    op_prefs = cfg.get("operation_preferences", {})

    parameters = [
        {"ParameterKey": k, "ParameterValue": v}
        for k, v in env_cfg["parameters"].items()
    ]

    cfn = boto3.client("cloudformation")

    common = dict(
        StackSetName=stackset_name,
        CallAs=call_as,
    )

    # 1. Crear o actualizar el StackSet
    try:
        cfn.describe_stack_set(**common)
        exists = True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("StackSetNotFoundException",):
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
            {"Key": "ManagedBy", "Value": "secdevops-pipeline"},
            {"Key": "Environment", "Value": args.env},
        ],
    )

    if not exists:
        print(f"Creando StackSet {stackset_name}...")
        cfn.create_stack_set(**stackset_args)
    else:
        print(f"Actualizando StackSet {stackset_name}...")
        try:
            op = cfn.update_stack_set(
                **stackset_args,
                OperationPreferences=op_prefs,
                DeploymentTargets={
                    "OrganizationalUnitIds": env_cfg["organizational_unit_ids"]
                },
                Regions=env_cfg["regions"],
            )
            wait_for_operation(cfn, stackset_name, op["OperationId"], call_as)
        except ClientError as e:
            if "No updates" in str(e):
                print("Sin cambios en el template/parametros.")
            else:
                raise

    # 2. Asegurar stack instances en las OUs objetivo
    instances = cfn.list_stack_instances(**common).get("Summaries", [])
    if not instances:
        print("Creando stack instances...")
        op = cfn.create_stack_instances(
            **common,
            DeploymentTargets={
                "OrganizationalUnitIds": env_cfg["organizational_unit_ids"]
            },
            Regions=env_cfg["regions"],
            OperationPreferences=op_prefs,
        )
        wait_for_operation(cfn, stackset_name, op["OperationId"], call_as)
    else:
        print(f"{len(instances)} stack instances existentes.")

    # 3. Reportar estado final
    for inst in cfn.list_stack_instances(**common).get("Summaries", []):
        print(
            f'  {inst["Account"]} / {inst["Region"]}: '
            f'{inst.get("StackInstanceStatus", {}).get("DetailedStatus", inst.get("Status"))}'
        )

    print("Despliegue completado.")


if __name__ == "__main__":
    main()
