# Cardkingdom clean-container probe

Runs the Codex-owned acceptance test `tests/acceptance_cardkingdom_container.py`
in a fresh Linux container. The image holds only that test file — no datasets,
app code or credentials — and runs it under `xvfb-run -a`, so Chromium can use a
normal headed window on a virtual display.

## Current flow: `--flow headed-naver-product`

The image's default command is `--flow headed-naver-product`; the workflow and
the local instructions pass it explicitly. In this flow the test uses:

- headed Chromium (`headless=False`) under Xvfb, with its sandbox enabled
  (`chromium_sandbox=True`) and the browser's unmodified request headers;
- one fresh browser context with no profile, imported cookies, proxy or stealth;
- one load of `https://www.naver.com`, then exactly one navigation to
  `https://smartstore.naver.com/cardkingdom/products/4960632716` (waiting for
  `load`), followed by the test's existing strict product checks.

Test contract: `python /probe/acceptance_cardkingdom_container.py --output /evidence --flow headed-naver-product`
writes `report.json` plus rendered text/screenshots and exits 0 only when that
product check succeeds, nonzero otherwise. `xvfb-run` returns the test's exit
status. Arguments after the image name replace the default command and are
passed to the test.

The report records the browser/OS versions, initial cookie count,
`navigator.webdriver`, NNB presence after the previsit (no cookie values),
document statuses and public headers, elapsed time, and cgroup peak memory when
the kernel exposes it. A pass covers this one known in-stock listing; shop
search, sold-out listings, repeatability and AWS are outside this run's scope.

Why this flow: on 2026-10-08 a local check from a home IP (recorded in
`HANDOFF.md`) found that direct product requests returned HTTP 429 in every
browser mode, while a headed Chromium window that first opened naver.com
received HTTP 200 and the product JSON-LD. Whether the same flow passes from a
GitHub-hosted runner or from AWS is **not yet verified**; no result for this flow
has been recorded here.

## Historical results (earlier flows, no longer run)

The two sections below record earlier headless runs. They describe the old
flows (`--header-profile baseline` / `chrome-headers`, home → search → two
products), not the current workflow.

### Experimental header comparison (2026-10-08)

After the first run's HTTP 429, the workflow then compared two conditions:

| Run | Profile | Meaning | Observed result |
|---|---|---|---|
| `run1` | `baseline` | The test's original request headers, unchanged | HTTP 429 at shop home |
| `run2` | `chrome-headers` | Chrome-like public HTTP request headers | HTTP 429 at shop home |

The Chrome-like profile changes only public HTTP client metadata. It does not
reproduce Chrome's TLS fingerprint or full browser identity, and no actual
Windows Chrome profile, cookies or session were copied. There is no User-Agent
rotation and no retry loop. Source identity checks and app-level validation stay
strict in both conditions.

[Actions run 37734622874](https://github.com/aziran07/ygoSolver/actions/runs/37734622874)
tested commit `6c36a5543ebe5a83eedcd3ceed9e6dfb4120c4d8` at 05:53–05:54 UTC.
Both cases used the same runner/image and started with zero cookies. The second
container started 30.34 seconds after the first finished. Both loaded the
network-smoke page with HTTP 200, then failed on the Cardkingdom home with HTTP
429 and no `Retry-After`. Neither reached search or product validation.

The recorded outgoing headers confirm these differences:

- `User-Agent`: `HeadlessChrome/153.0.8010.12` became `Chrome/153.0.0.0`.
- `Accept-Language`: absent in the baseline, then
  `ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7`.
- `Sec-CH-UA`: the HeadlessChrome brand was replaced with Google Chrome; the
  constructed brand list was verified against the actual outgoing request.
- Both cases already sent the browser's native `Accept`, `Accept-Encoding`,
  `Sec-Fetch-*`, `Upgrade-Insecure-Requests`, Linux platform and desktop hints.

All five configured header values were verified before interpreting the HTTP
response. Reports and screenshots independently confirm that this particular
public-header combination did not resolve the 429. This does not identify the
blocking mechanism or rule out every possible header-related cause. The job
retains both failed steps; a successful evidence upload is not a successful
price lookup. The initial single-container run is recorded below separately.

### First single-container run (2026-10-08)

[Actions run 37733371611](https://github.com/aziran07/ygoSolver/actions/runs/37733371611)
tested commit `9bad15bb5a57cdeb96c9d607e63ef36ef5992f70` at 05:38 UTC.
The image built successfully and Chromium 153.0.8010.12 launched on Ubuntu
24.04.4 in Docker as UID 1001, with sandbox requested and zero initial cookies.
The browser loaded `https://example.com/` with HTTP 200.

The first Cardkingdom home document returned **HTTP 429** and rendered a service
unavailable page. The test exited 1 at `shop_home`; search, both product checks,
and the second fresh-container run were **not reached**. The workflow remains
failed, with the report, rendered text, screenshot and runtime metadata saved
as evidence. This proves the container can run the browser, but does not
establish an unattended Cardkingdom price lookup path. The reason for the 429
(network, browser characteristics, session state, or another factor) remains
undetermined; AWS accessibility has not been tested. No bypass or retry was used.

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
docker run --rm --init --shm-size=1g --cpus 1 --memory 2g --user pwuser \
  --security-opt seccomp=tools/cardkingdom_probe/seccomp_profile.json \
  -v "$PWD/$work/probe-output/run1:/evidence" cardkingdom-probe --flow headed-naver-product
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
browser and Xvfb zombie processes; `--cpus 1 --memory 2g` fix the resource
limits so runs are comparable; `--shm-size=1g` avoids Chromium crashes from the 64 MB
default `/dev/shm`. No `-e`/`--env-file` is passed, so no host secrets reach the
container. On Ubuntu 24.04+ hosts, AppArmor may block unprivileged user
namespaces and therefore the sandbox; that is a host failure to report, not a
reason to disable the sandbox.

## GitHub Actions

`.github/workflows/cardkingdom-probe.yml` runs on pushes to `feature/card-prices`
touching the test, `Dockerfile`, `seccomp_profile.json` or the workflow, or
manually via `workflow_dispatch` (no schedule or pull-request trigger).
README-only changes do not trigger a store probe. It builds the image once, then
runs exactly one fresh container (`run1`, `--flow headed-naver-product`,
`--cpus 1 --memory 2g`). There is no second run, retry or `continue-on-error`;
a probe failure fails the job. The `probe-output` folder (`run1/`,
`metadata.txt`, which records the flow and resource limits) is always uploaded
as artifact `cardkingdom-probe-evidence` (3-day retention); HAR, zip/trace,
`.js`, cookie/token/storage files are rejected (job fails) and excluded from
upload.

Download evidence:

```sh
gh run list --workflow cardkingdom-probe.yml --branch feature/card-prices
gh run download <run-id> -n cardkingdom-probe-evidence -D evidence
```

## What this does not prove

The GitHub-hosted runner's network is not AWS. Success there would show the flow
works in a clean Linux container with headed Chromium under Xvfb; it does
not show the store accepts requests from an AWS region/IP range, from Korean or
other specific egress, at production frequency, or over time. Store responses
(e.g. HTTP 429 or a login redirect) can depend on source IP reputation, so AWS
accessibility needs the same image run from the target AWS environment.
