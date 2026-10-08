# Cardkingdom clean-container probe

Runs the Codex-owned acceptance test `tests/acceptance_cardkingdom_container.py`
in a fresh Linux container: headless Chromium with its sandbox enabled, no
browser profile, cookies, tokens, proxies or stealth. The image holds only that
test file — no datasets, app code or credentials.

Test contract: `python /probe/acceptance_cardkingdom_container.py --output /evidence`
writes `report.json` plus rendered text/screenshots, exits 0 only when the
browser search and both product checks succeed, nonzero otherwise.

## Pinned inputs

| Input | Value | Source |
|---|---|---|
| Base image | `mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a…a1f0` | <https://playwright.dev/python/docs/docker> |
| Python package | `playwright==1.63.0` (base image ships browsers, not the system-Python package) | PyPI |
| Seccomp profile | `seccomp_profile.json`, verbatim copy of `https://raw.githubusercontent.com/microsoft/playwright/v1.63.0/utils/docker/seccomp_profile.json`, sha256 `cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849` | Playwright repo |
| Runner | GitHub `ubuntu-22.04` | — |
| Actions | `actions/checkout` v7.0.1, `actions/upload-artifact` v7.0.2, pinned by commit SHA | GitHub |

## Run locally (any Linux host with Docker)

From the repository root:

```sh
work=data/experiments/cardkingdom_container
mkdir -p "$work/build-context" "$work/probe-output/run1"
cp tests/acceptance_cardkingdom_container.py "$work/build-context/"
docker build --pull -t cardkingdom-probe -f tools/cardkingdom_probe/Dockerfile "$work/build-context"
chmod 0777 "$work/probe-output/run1"
docker run --rm --init --shm-size=1g --user pwuser \
  --security-opt seccomp=tools/cardkingdom_probe/seccomp_profile.json \
  -v "$PWD/$work/probe-output/run1:/evidence" cardkingdom-probe
echo "exit: $?"
```

`data/experiments/` is git-ignored, so generated files stay out of commits.
The build context holds only the test, keeping the repository (including
ignored local datasets) out of the image. The test requires an empty evidence
folder; use a new `runN` folder per run.

Sandbox constraints: the container runs as non-root `pwuser`; Chromium's
sandbox is allowed by the official seccomp profile (it permits the namespace
syscalls the sandbox needs). Never run with `--privileged`,
`--cap-add=SYS_ADMIN`, `--no-sandbox` or `seccomp=unconfined`. `--init` reaps
browser zombie processes; `--shm-size=1g` avoids Chromium crashes from the 64 MB
default `/dev/shm`. No `-e`/`--env-file` is passed, so no host secrets reach the
container. On Ubuntu 24.04+ hosts, AppArmor may block unprivileged user
namespaces and therefore the sandbox; that is a host failure to report, not a
reason to disable the sandbox.

## GitHub Actions

`.github/workflows/cardkingdom-probe.yml` runs on pushes to `feature/card-prices`
touching the test, `Dockerfile`, `seccomp_profile.json` or the workflow, or
manually via `workflow_dispatch` (no schedule or pull-request trigger).
README-only changes do not trigger a store probe. It builds the image, runs one fresh container, and runs a
second fresh container only if the first succeeded (cold-start
reproducibility). Any probe failure fails the job. The `probe-output` folder
(`run1/`, `run2/`, `metadata.txt`) is always uploaded as artifact
`cardkingdom-probe-evidence` (3-day retention); HAR, zip/trace, `.js`,
cookie/token/storage files are rejected (job fails) and excluded from upload.

Download evidence:

```sh
gh run list --workflow cardkingdom-probe.yml --branch feature/card-prices
gh run download <run-id> -n cardkingdom-probe-evidence -D evidence
```

## What this does not prove

The GitHub-hosted runner's network is not AWS. Success there shows the flow works in a clean headless Linux container; it does
not show the store accepts requests from an AWS region/IP range, from Korean or
other specific egress, at production frequency, or over time. Store responses
(e.g. HTTP 429 or a login redirect) can depend on source IP reputation, so AWS
accessibility needs the same image run from the target AWS environment.
