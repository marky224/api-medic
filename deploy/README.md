# Deploying the api-medic hosted demo

The hosted demo at `https://api-medic.markandrewmarquez.com` exposes
both captured-mode and live-run paths:

- `POST /api/analyze` — HAR/curl input, runs through the engine
- `POST /api/run` — live request execution. SSRF-guarded (rejects
  RFC1918, link-local incl. EC2 metadata, multicast, loopback, http://,
  and connects only to the address it checked, so DNS rebinding can't
  swap in another); 10 s per-request timeout; 2 req/sec sustained throttle (burst 5)
  via API Gateway; Lambda capped at 5 concurrent executions.

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
   Its trust and inline policies live in `deploy/iam/` and are applied
   by hand, not by CI. The inline policy's role statements cover only
   `role/api-medic-demo-ApiAnalyzeFunctionRole-*`, which is SAM's
   generated function role: SAM gives it the logical ID
   `<function logical ID>Role`, and CloudFormation names it
   `<stack>-<logical ID>-<unique id>`. They can't touch the deploy role
   or any other `api-medic-demo-*` role. On that role, the deploy role
   can attach only `AWSLambdaBasicExecutionRole`, can pass it only to
   Lambda, and can't add inline policies. That's all the role needs
   while `ApiAnalyzeFunction` has no `Policies` (or other properties that
   make SAM add policies). Renaming the stack or the function, or adding
   policies, means widening `inline-policy.json` first.
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

`.github/workflows/deploy-demo.yml` runs on `workflow_run` when the CI
workflow completes, not on the push itself. The deploy job proceeds only
when all of these hold:

- the repo variable `DEMO_DEPLOY_ENABLED` is `true`,
- the CI run concluded `success` (the integration job is
  `continue-on-error`, so a live-network flake there doesn't block it),
- the CI run was for a `push` event,
- its head branch is `main`, and
- its commit (`workflow_run.head_sha`) is still `main`'s tip
  (`github.sha`, which is `main`'s head when the deploy run is
  triggered).

It checks out `workflow_run.head_sha`, the exact commit CI tested. If CI
fails, nothing deploys. Re-running CI to green triggers the deploy only
if that commit is still `main`'s tip; once a newer commit has landed, its
own CI run is the one that deploys. The same check stops an older commit
from deploying over a newer one when two pushes finish CI out of order.

Deploys run one at a time (job-level `concurrency: deploy-demo`, with
`cancel-in-progress: false`). A running deploy is never cancelled; a
newer pending deploy replaces an older pending one. The group is on the
job, not the workflow, because every CI completion starts a run of this
workflow; at workflow level, the runs that get skipped (PR CI, for
example) would still join the group and cancel a pending deploy.

`workflow_run` also fires for pull_request CI runs, including fork PRs,
and always runs the workflow file from `main` with secrets and
`id-token: write`. The job-level `if` is what keeps PR code away from
AWS: a pull_request run fails the `event == 'push'` check. The
`branches: [main]` filter alone isn't enough, because a fork PR from a
branch named `main` matches it. The OIDC trust policy isn't a backstop
here either: a `workflow_run` job's token carries the default branch ref,
so it matches `ref:refs/heads/main` whatever triggered it.

Once triggered, the workflow:

1. Builds the frontend with `VITE_DEMO_MODE=1` and runs `sam build`,
   without AWS credentials.
2. Assumes the `AWS_DEPLOY_ROLE_ARN` via OIDC.
3. Runs `sam deploy` with the secrets as parameter overrides.
4. Syncs `frontend/dist/` to the S3 bucket and invalidates CloudFront.

The role is assumed only after the build because `npm ci` runs
third-party install scripts, `sam build` pip-installs packages, and
`configure-aws-credentials` exports the role's keys to every later step.
That keeps ready-made keys away from package code. It isn't full
isolation: with `id-token: write` on the job, any step can ask the
runner for an OIDC token (`ACTIONS_ID_TOKEN_REQUEST_URL` and
`ACTIONS_ID_TOKEN_REQUEST_TOKEN`) and exchange it for the role if it
knows the role ARN. Building in a separate job without `id-token: write`
would close that.

The workflow is also dispatchable manually from the Actions tab. Manual
dispatch skips the `DEMO_DEPLOY_ENABLED` and CI checks and deploys the
dispatched ref (the trust policy only accepts `main`).

## Tearing down

```bash
cd deploy
# Empty the S3 bucket first — CloudFormation refuses to delete a non-empty bucket.
aws s3 rm s3://<WebBucketName> --recursive
sam delete --stack-name api-medic-demo --region us-east-1
```

## Architectural invariants for this stack

- The Lambda imports `pydantic`, `uncurl`, `httpx`, `dnspython`,
  `cryptography`, and `api_medic.core` — **no fastapi, uvicorn, rich,
  or typer**. Verified by `tests/unit/test_lambda_imports.py`.
- `/api/run` MUST go through `core.runner_safety.check_url_safe`
  before any socket is opened. Verified by an explicit assertion in
  the same test module.
- `/api/run` MUST open every connection (TLS probe, request, redirect
  hops) through `core.runner_safety`'s pinned connector
  (`run(..., pin_connections=True)`), so each socket goes to an address
  checked in the same step. Verified by `TestRunConnectionPinning` in
  `tests/unit/test_lambda_handler.py`.
- **No persistence**: no DynamoDB, no S3 writes for user data, no
  CloudWatch persistence beyond the function's 14-day log retention.
- **No request data in the logs** (see `PRIVACY.md`): the handler logs
  an unhandled exception's type and location only, never its message,
  and answers a plain 500; the httpx logger is held at WARNING so request
  URLs aren't logged; `parse_curl` swallows argparse's stderr, which
  quotes rejected curl arguments. Verified by `TestUnhandledErrors` and
  the stderr and logging tests in `tests/unit/test_lambda_handler.py`.

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
