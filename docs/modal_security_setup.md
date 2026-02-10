# Modal Security Setup Guide

This guide covers secure setup for Modal authentication, secrets, and CI/CD deployment.

## Scope

- Local developer authentication on laptops/workstations.
- CI/CD authentication with non-personal credentials.
- Secret storage patterns for S3 and other external systems.
- A copy-paste GitHub Actions workflow template with secure defaults.

## 1) Local Developer Auth

Install and authenticate Modal locally:

```bash
python -m pip install -U modal
modal setup
modal token info
```

Recommended controls:

- Use personal developer tokens only for local/dev usage.
- Do not copy local token files into project directories.
- Rotate personal tokens on a schedule and immediately on device loss.

## 2) Environment Separation

Create isolated Modal environments:

```bash
modal environment create dev
modal environment create prod
modal config set-environment dev
```

Recommended controls:

- Keep `dev` and `prod` resources separate.
- Apply stricter access controls to `prod`.
- Use explicit `MODAL_ENVIRONMENT` in automation.

## 3) Secret Management

Create secrets in Modal (not in repo files):

```bash
modal secret create -e dev my-app-secrets --from-dotenv /secure/path/dev.env
modal secret create -e prod my-app-secrets --from-dotenv /secure/path/prod.env
modal secret list -e dev --json
```

Recommended controls:

- Never commit `.env` files.
- Keep sensitive values out of `config/gpu_backend.toml`.
- Use different secret values for dev/prod.
- Prefer short-lived and least-privilege cloud credentials.

Attach secrets to Modal functions/classes:

```python
import modal

app = modal.App("rentagpu-executor")
runtime_secret = modal.Secret.from_name(
    "my-app-secrets",
    required_keys=["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
)

@app.function(secrets=[runtime_secret])
def run_gpu_job(payload: dict) -> dict:
    ...
```

## 4) Token Strategy for CI/CD

Use Modal Service User tokens for CI, not personal tokens.

Create CI variables/secrets in GitHub:

- Repository variables:
  - `MODAL_APP_NAME` (example: `rentagpu-executor`)
- Environment secrets in `dev` and `prod` GitHub Environments:
  - `MODAL_TOKEN_ID`
  - `MODAL_TOKEN_SECRET`

Recommended controls:

- Use GitHub Environment protection rules for `prod` (required reviewers).
- Restrict deployment to `main` branch for production.
- Rotate CI service-user tokens regularly and on team/offboarding events.

## 5) Copy-Paste GitHub Actions Template

Save as `.github/workflows/modal_deploy.yml` in the target repo.

```yaml
name: modal-deploy

on:
  push:
    branches: ["main"]
    paths:
      - "scripts/gpu_modal_app.py"
      - "config/gpu_backend.toml"
      - ".github/workflows/modal_deploy.yml"
  workflow_dispatch:
    inputs:
      target_environment:
        description: "Modal environment to deploy to"
        type: choice
        required: true
        default: "dev"
        options:
          - dev
          - prod

permissions:
  contents: read

concurrency:
  group: modal-deploy-${{ github.ref }}
  cancel-in-progress: false

jobs:
  deploy_dev_on_push:
    if: ${{ github.event_name == 'push' }}
    runs-on: ubuntu-latest
    environment: dev
    env:
      MODAL_ENVIRONMENT: dev
      MODAL_TOKEN_ID: ${{ secrets.MODAL_TOKEN_ID }}
      MODAL_TOKEN_SECRET: ${{ secrets.MODAL_TOKEN_SECRET }}
      MODAL_APP_NAME: ${{ vars.MODAL_APP_NAME }}
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install Modal CLI
        run: python -m pip install --upgrade modal

      - name: Verify auth
        run: modal token info

      - name: Deploy app
        run: modal deploy scripts/gpu_modal_app.py

  deploy_manual:
    if: ${{ github.event_name == 'workflow_dispatch' }}
    runs-on: ubuntu-latest
    environment: ${{ inputs.target_environment }}
    env:
      MODAL_ENVIRONMENT: ${{ inputs.target_environment }}
      MODAL_TOKEN_ID: ${{ secrets.MODAL_TOKEN_ID }}
      MODAL_TOKEN_SECRET: ${{ secrets.MODAL_TOKEN_SECRET }}
      MODAL_APP_NAME: ${{ vars.MODAL_APP_NAME }}
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install Modal CLI
        run: python -m pip install --upgrade modal

      - name: Guard production branch
        if: ${{ inputs.target_environment == 'prod' && github.ref != 'refs/heads/main' }}
        run: |
          echo "Production deploys are only allowed from main."
          exit 1

      - name: Verify auth
        run: modal token info

      - name: Deploy app
        run: modal deploy scripts/gpu_modal_app.py
```

## 6) Security Checklist

- `No plaintext secrets in repo`: no keys in code, TOML, or docs examples.
- `Environment isolation`: separate `dev` and `prod` secrets and approvals.
- `Least privilege`: CI tokens scoped for deployment only.
- `Rotation`: scheduled and event-driven token/key rotation.
- `Auditing`: review workflow runs and token usage regularly.

## 7) Incident Response

If a token or secret is exposed:

1. Revoke compromised Modal token immediately.
2. Rotate affected cloud/API keys and update Modal Secrets.
3. Re-run deployment with fresh credentials.
4. Review audit logs and repository history for further leakage.
