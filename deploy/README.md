# Deploying the api-medic hosted demo

The hosted demo at `https://api-medic.markandrewmarquez.com` is a
captured-mode-only build — the Lambda surface accepts HAR uploads and
curl strings via `POST /api/analyze`, runs them through the engine, and
returns a `Report`. There is no live runner on this surface by design.

Architecture: S3 (React build) + CloudFront + API Gateway HTTP API +
Lambda. DNS is managed by Route 53.

## One-time prerequisites

1. **AWS account** with IAM permissions to deploy Lambda, API Gateway,
   S3, CloudFront, Route 53, and IAM roles.
2. **ACM certificate** in **us-east-1** (CloudFront requires the cert
   there) covering `api-medic.markandrewmarquez.com`. Issue it via
   `aws acm request-certificate --domain-name api-medic.markandrewmarquez.com
   --validation-method DNS --region us-east-1`, then add the validation
   CNAME to the `markandrewmarquez.com` Route 53 hosted zone. Wait for
   the cert to issue (status `ISSUED`).
3. **GitHub OIDC role** so the deploy workflow can assume an IAM role
   without long-lived keys. The role's trust policy should allow
   `sts:AssumeRoleWithWebIdentity` from `repo:marky224/api-medic:ref:refs/heads/main`.
4. **GitHub Actions secrets** in the repo:
   - `AWS_DEPLOY_ROLE_ARN` — the OIDC role's ARN
   - `ACM_CERTIFICATE_ARN` — the ARN of the cert from step 2
   - `ROUTE53_HOSTED_ZONE_ID` — the hosted zone ID for `markandrewmarquez.com`
     (visible in the Route 53 console; 21-char string starting with `Z`)

## Deploying manually (one-shot)

From the repo root, with AWS CLI configured for us-east-1:

```bash
cd deploy
sam build
sam deploy \
  --parameter-overrides \
    HostedZoneId=Z0123456789ABCDEFGHIJ \
    CertificateArn=arn:aws:acm:us-east-1:111111111111:certificate/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

That creates the CloudFormation stack. Stack outputs include
`WebBucketName` (S3 bucket for the React build) and `DistributionId`
(for cache invalidation).

After the first deploy, sync the React build:

```bash
cd ../frontend
VITE_DEMO_MODE=1 npm run build
aws s3 sync dist/ s3://<WebBucketName> --delete
aws cloudfront create-invalidation --distribution-id <DistributionId> --paths "/*"
```

## Deploying via GitHub Actions

Pushes to `main` trigger `.github/workflows/deploy-demo.yml`, which:

1. Assumes the `AWS_DEPLOY_ROLE_ARN` via OIDC.
2. Runs `sam build` and `sam deploy` with the secrets as parameter overrides.
3. Builds the frontend with `VITE_DEMO_MODE=1`.
4. Syncs `frontend/dist/` to the S3 bucket and invalidates CloudFront.

The workflow is also dispatchable manually from the Actions tab.

## Tearing down

```bash
cd deploy
# Empty the S3 bucket first — CloudFormation refuses to delete a non-empty bucket.
aws s3 rm s3://<WebBucketName> --recursive
sam delete --stack-name api-medic-demo --region us-east-1
```

## Architectural invariants for this stack

- The Lambda imports only `pydantic`, `uncurl`, and `api_medic.core` —
  **no httpx, fastapi, uvicorn, dnspython, or cryptography**. Verified
  by `tests/unit/test_lambda_imports.py`.
- The hosted demo is **captured-mode only** — there is no `/api/run`
  endpoint. The frontend's Run tab is hidden via `VITE_DEMO_MODE=1`.
- **No persistence**: no DynamoDB, no S3 writes for user data, no
  CloudWatch persistence beyond the Lambda's default execution log.

## Building locally

The Lambda build runs in a Linux container (required for pydantic's
native compiled extensions; building on Windows or macOS would package
the wrong-platform wheel into the zip and crash at runtime in Lambda).

The container only sees the `deploy/lambda/` directory, so `api_medic`
must be vendored in before each build:

PowerShell:

    Copy-Item -Recurse -Force ..\src\api_medic .\lambda\api_medic
    sam build

Bash:

    cp -r ../src/api_medic lambda/api_medic
    sam build

The vendored copy is gitignored. Cleaning it up after a build is
optional; the next vendor-and-build cycle will overwrite it.
