# infra/: CDK app for the data plane and pipelines

See `docs/decisions.md` D2 and D3. Region us-east-2. Tag everything `project=fieldwatch`.

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
