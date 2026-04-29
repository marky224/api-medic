# Publishing api-medic to PyPI

The `.github/workflows/publish-pypi.yml` workflow builds the wheel + sdist
and publishes to PyPI on every `v*` tag push. It uses **trusted publishing**
via OIDC — no PyPI API token is stored in this repo's secrets. PyPI verifies
the upload by checking the GitHub-issued OIDC token against a pre-configured
publisher binding.

## One-time setup

These steps are required **before the first publish only**. They establish
the trust binding between PyPI and this repo.

### 1. PyPI account

If you don't have a PyPI account, create one at <https://pypi.org/account/register/>
and verify the email. Enable 2FA when prompted.

### 2. Add a Pending Publisher on PyPI

Because the `api-medic` project doesn't exist on PyPI yet, register it as a
*pending publisher* — PyPI lets you bind the OIDC trust *before* the first
upload, then claims the project name on first successful publish.

1. Go to <https://pypi.org/manage/account/publishing/>.
2. Click **"Add a new pending publisher"**.
3. Fill in exactly:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `api-medic` |
   | Owner | `marky224` |
   | Repository name | `api-medic` |
   | Workflow name | `publish-pypi.yml` |
   | Environment name | `pypi` |

4. Click **Add**.

### 3. (Recommended) Create the GitHub Environment

The workflow runs in an `environment: pypi` block. Creating that environment
in the repo lets you add protection rules later (manual approval before
publish, allowed branches/tags, etc.) without touching the workflow.

1. Repo → **Settings → Environments → New environment**.
2. Name: `pypi`.
3. Save with no protection rules for now (or add `Required reviewers = marky224`
   if you want a manual approval gate per publish).

## First publish (v1.0.0)

The `v1.0.0` tag is already on origin but the publish workflow didn't exist
when it was created, so no publish fired. Trigger it manually:

```bash
gh workflow run publish-pypi.yml --ref v1.0.0 -f ref=v1.0.0
```

Watch the run:

```bash
gh run watch $(gh run list --workflow publish-pypi.yml --limit 1 --json databaseId --jq '.[0].databaseId')
```

After it completes, `pip install api-medic` should pull `1.0.0`.

## Subsequent releases

Going forward, just create and push a tag:

```bash
git tag -a v1.1.0 -m "v1.1.0 release notes here"
git push origin v1.1.0
```

The workflow fires automatically on the tag push, builds, and publishes.
No manual `twine upload` ever again.

## Troubleshooting

- **`InvalidPublisher` from PyPI**: the project name, owner, repo, workflow
  name, or environment in the Pending Publisher form doesn't match exactly.
  Re-check; PyPI is case-sensitive on workflow filenames.
- **`File already exists`**: PyPI doesn't allow re-uploading the same
  version. Bump `pyproject.toml` `version` and re-tag.
- **Wheel missing the React bundle**: confirm the workflow's `Stage frontend
  into wheel source tree` step ran. The wheel relies on `force-include` of
  `src/api_medic/web/frontend/`, which is gitignored and populated at build
  time.
