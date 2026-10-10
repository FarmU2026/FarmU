# infra/: CDK app for the data plane and pipelines

See `docs/decisions.md` D2 and D3. Caleb's account, region **us-west-2**. Tag everything `project=fieldwatch`.

This account also hosts another project. Never create anything named `preflight-*` or `pr-*`, never use the tag `preflight-derive`, and never create or modify the account's GitHub OIDC provider (we only reference it). The `fieldwatch-boundary` permissions boundary enforces this for every FieldWatch role, user and the CDK execution role.

## One-time setup (integrator, done once)

```powershell
# 1. The boundary policy (must exist before bootstrap and before FieldWatch-Access)
aws iam create-policy --policy-name fieldwatch-boundary --policy-document file://policies/fieldwatch-boundary.json --tags Key=project,Value=fieldwatch

# 2. Bootstrap CDK with the boundary on its CloudFormation execution role (creates a ~$1/month KMS key)
npx aws-cdk@2 bootstrap --custom-permissions-boundary fieldwatch-boundary --tags project=fieldwatch

# 3. Shared stacks
npx aws-cdk@2 deploy FieldWatch-DataPlane FieldWatch-Access
```

Then in GitHub (repo admin): Settings → Secrets and variables → Actions → **Variables**: `AWS_DEPLOY_ROLE_ARN` (the `fieldwatch-github-deploy` role ARN) and `AWS_REGION=us-west-2`; Settings → Environments → create **deploy**. Kurt and Aaron: in the IAM console, give `fieldwatch-kurt` / `fieldwatch-aaron` a console password (they can then create their own access keys) and have them assume `fieldwatch-dev`.

```powershell
cd infra
python -m pip install -r requirements.txt
npx aws-cdk@2 synth                      # no AWS calls; checks the templates
npx aws-cdk@2 deploy FieldWatch-DataPlane   # integrator only
npx aws-cdk@2 deploy FieldWatch-<Yours>     # your workstream's stack only
```

Rules:
- Deploy only your own stack. Never `cdk destroy` a stack you don't own.
- Bootstrap the account once, deliberately: `npx aws-cdk@2 bootstrap`. It creates a KMS key (~$1/month), a versioned bucket and an ECR repo that are kept when the bootstrap stack is deleted (spike finding 36).
- Lambdas are x86_64, Python 3.12. Analysis code from `agent/fieldwatch/` is packaged as-is, so keep it free of web-app imports.

One manual step (CloudFormation can't create SecureStrings), done once by the integrator:

```powershell
aws ssm put-parameter --name /fieldwatch/link-signing-key --type SecureString --value (python -c "import secrets; print(secrets.token_urlsafe(48))")
```

Teardown: `npx aws-cdk@2 destroy --all`, then follow spike finding 38 for anything created outside CDK.
