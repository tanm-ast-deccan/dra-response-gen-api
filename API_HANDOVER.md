# dra_harness HTTP API — deployment handover

For whoever hosts the service. Assumes no knowledge of the DRA project.

---

## 1. What this is

An HTTP wrapper around a long-running LLM research pipeline. A client submits a
research task; the service drives one or more frontier models through an
agentic loop (web search, file reading, code execution) and returns the
resulting report plus any generated deliverable (xlsx / docx / pptx / pdf).

**A single run takes minutes to hours.** So this is a *job* API. Submission
returns a `run_id` immediately; the client polls for completion. No HTTP
request stays open for the duration of a run — request timeouts can be short
(30s is plenty), but the app itself must not be killed between requests.

---

## 2. Read this before deploying

**The agent executes model-generated code.** `tools.python_execute` and
`tools.bash_execute` run arbitrary Python and shell commands produced by a
language model, on the machine running this service. That is the intended
design — the models need it to build spreadsheets and run calculations — but it
means:

- The container is the security boundary. Run it as the provided Dockerfile
  does: non-root, `no-new-privileges`, `cap_drop: ALL`, memory and pid limits.
- Do not run this on a host holding unrelated secrets or customer data.
- Do not mount host directories read-write beyond the `/data` volume.
- Do not run with `--privileged` or with a mounted Docker socket.
- Egress is required (OpenRouter, web search, Google Drive). The agent's
  web-fetch tool needs broad internet access to work.

**It costs real money.** Every `live: true` job bills the OpenRouter account
behind `OPENROUTER_API_KEY`. The default is `live: false` (dry-run, free) so a
malformed request cannot spend anything, and there is a per-run budget guard
(`max_cost_usd`, default $50). Set a hard spend cap on the OpenRouter account
itself as well — that is the only limit the service cannot bypass.

---

## 3. Authentication

Requests from the machine running the service (127.0.0.1 / ::1) need no
credentials. Requests from any other address must send `X-API-Key` matching
`DRA_API_KEY`, and are refused with 403 if that variable is unset.

**For any hosted deployment this means `DRA_API_KEY` is mandatory** — a
container receives every request from outside itself, so nothing would work
without it, and an unauthenticated endpoint with shell access is not something
to leave running.

`DRA_API_KEY` is not issued by anyone. Generate it: `openssl rand -hex 32`.
It is unrelated to `OPENROUTER_API_KEY`, which comes from OpenRouter and does
the billing.

---

## 4. Run it

### With Docker (how this is meant to be hosted)

```bash
cp .env.api.example .env
# fill in DRA_API_KEY and OPENROUTER_API_KEY, and set DRA_RUN_ROOT
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

The compose file runs the server for you. The command it uses is in the
Dockerfile's `CMD`.

### Without Docker (development, or a plain VM)

```bash
pip install -r requirements-api.txt

export DRA_RUN_ROOT="$PWD/runs_dir"        # absolute, writable — NOT /data/runs
export OPENROUTER_API_KEY=sk-or-...        # omit for dry runs only
export DRA_API_KEY=$(openssl rand -hex 32) # only needed for non-loopback access

uvicorn dra_harness.api:app --host 127.0.0.1 --port 8000 --workers 1
```

Run it from the repo root, in the foreground — tracebacks from failed jobs
appear there and nowhere else. `--workers 1` is required (see below). Use
`--host 0.0.0.0` only with `DRA_API_KEY` set and a reverse proxy in front.

Port already in use means an older server is still running:
`lsof -tiTCP:8000 -sTCP:LISTEN | xargs kill`.

Generated API docs at `/docs`. A dashboard that polls the endpoints for you at
`/`.

### Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `DRA_RUN_ROOT` | yes | Persistent, **writable** volume. Run folders, deliverables, results JSON. |
| `DRA_API_KEY` | yes when hosted | Shared secret for non-loopback requests. |
| `OPENROUTER_API_KEY` | for live runs | Billing account for all model calls. |
| `DRA_MAX_PARALLEL_JOBS` | no (2) | Concurrent jobs. See sizing below. |
| `GOOGLE_SERVICE_ACCOUNT_KEY` | for Drive inputs | Path to a service-account JSON. |
| `SERPER_API_KEY` | no | Better web search than the DuckDuckGo fallback. |

`DRA_RUN_ROOT` is the variable that breaks deployments. The container default
is `/data/runs`, backed by the compose volume. Running outside Docker it must
be an absolute path you can write to — `/data` does not exist on macOS and the
first job dies with `Read-only file system`. `GET /health` reports `run_root`
and `run_root_writable`; check both before submitting anything.

Note that env vars set in the real environment always beat `.env` — the loader
never overwrites an existing variable. A stale `export DRA_RUN_ROOT=...` in a
shell profile silently wins over the file.

### Hosting requirements

- **A long-lived container or VM.** Render, Railway, Fly.io, ECS/Fargate, or a
  plain VPS. Serverless request platforms (Vercel, Lambda, Cloud Run with
  scale-to-zero) do **not** work: the HTTP request returns in milliseconds and
  the platform then kills the instance while the job is still running.
- **`--workers 1`.** Job state lives in process memory. A second worker sees a
  different job table and `GET /run/{id}` starts 404-ing at random. Scale by
  raising `DRA_MAX_PARALLEL_JOBS` and the instance size, not by adding workers.
- **A persistent disk at `DRA_RUN_ROOT`.** Ephemeral filesystems lose every
  completed run on redeploy. 20 GB is a reasonable start.
- **RAM.** Roughly 1 GB per concurrent job plus headroom for the agent's code
  execution. `DRA_MAX_PARALLEL_JOBS=2` on a 4 GB instance is a safe start. Each
  job may itself fan out across several models (the request's `concurrency`
  field), so 2 jobs x 4 models is 8 concurrent model sessions.
- **TLS.** The compose file binds to loopback deliberately. Terminate TLS at a
  reverse proxy. `DRA_API_KEY` travels in a header and must not cross plain
  HTTP.

---

## 5. The API

Three endpoints, plus `/health` and the dashboard.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/run` | Queue a run → `{"run_id": ..., "status": "queued"}` |
| `GET` | `/run/{id}` | Status, cost summary, error |
| `GET` | `/run/{id}/result` | Report text, citations, cost, output file paths |
| `GET` | `/runs` | The 50 most recent jobs |
| `GET` | `/health` | Liveness, config, provider list |
| `GET` | `/` | Dashboard |

`POST /run` takes **exactly one** of `prompt` (a single inline task) or
`csv_path` (a CSV of tasks). Sending both or neither is a 422.

```bash
# one task
curl -X POST https://host/run \
  -H "X-API-Key: $DRA_API_KEY" -H "Content-Type: application/json" \
  -d '{"prompt": "Analyse FY25 unit economics and present the results in an Excel workbook.",
       "drive_url": "https://drive.google.com/drive/folders/...",
       "providers": ["opus5"], "live": true}'

# a CSV of tasks
curl -X POST https://host/run \
  -H "X-API-Key: $DRA_API_KEY" -H "Content-Type: application/json" \
  -d '{"csv_path": "/data/inputs/deliverable_tasks.csv",
       "providers": ["opus5"], "live": true, "max_rows": 5}'
```

Other fields: `task_id`, `output_format` (`xlsx`/`docx`/`pptx`/`pdf` —
authoritative; omit to detect from the prompt), `task_ids`, `passes`,
`concurrency`, `web_search`, `max_turns`, `max_cost_usd`, `model_overrides`.

`resolve_files` defaults to **true**: a task's `drive_url` is downloaded into
the run's staging directory before the model starts. That needs
`GOOGLE_SERVICE_ACCOUNT_KEY`, and the Drive folders must be shared with the
service account's email address. Without it, resolution yields zero files and
the run still "succeeds" with the model working blind — a silent failure worth
checking for on the first live run.

`status` moves `queued → running → succeeded | failed`. Poll every 15–30s.

### Client sketch

```python
import time, requests

H = {"X-API-Key": KEY}
rid = requests.post(f"{BASE}/run", headers=H, json={
    "prompt": PROMPT, "providers": ["opus5"], "live": True,
}).json()["run_id"]

while True:
    job = requests.get(f"{BASE}/run/{rid}", headers=H).json()
    if job["status"] in ("succeeded", "failed"):
        break
    time.sleep(20)

if job["status"] == "failed":
    raise RuntimeError(job["error"])

out = requests.get(f"{BASE}/run/{rid}/result", headers=H).json()
print(out["responses"][0]["response_text"])
```

---

## 6. Known limits

Design choices, not bugs to file:

- **No cancellation.** Python cannot safely kill a worker thread
  mid-`subprocess.run`. Killing a run means restarting the process.
- **Job state is in-process.** A restart loses the job table; completed runs
  survive on the volume as results JSON. Multi-instance deployment needs Redis
  or a database first.
- **No streaming.** The client polls. Progress inside a run is not visible
  until it finishes; container logs are the only live view.
- **No per-client quotas.** One shared key, no rate limiting, no per-caller
  cost attribution.
- **No uploads.** Inputs come from Drive links or paths already on the server.
- **Drive folder listing is not recursive.** Files in subfolders of a task's
  `drive_url` are silently skipped.

---

## 7. What to hand over

Ship a **git repository**, not a zip.

```
dra_harness/          the package (api.py, pipeline.py, runner.py, tools.py, ...)
requirements-api.txt  pinned runtime deps
Dockerfile
docker-compose.yml
.dockerignore
.env.api.example      documented, NO values filled in
API_HANDOVER.md       this file
```

Leave out: `src/`, the scoring and analysis scripts, `scratch.py`, every real
prompt CSV, `test_files/`, and all result artifacts. None of it is needed to
serve the API, and the prompts are the asset.

Before pushing:

- [ ] `.env` is gitignored and no key was ever committed. Check the history:
      `git log --all --full-history -- '*.json' .env | head`, and
      `git log -p | grep -iE 'sk-(or-)?[a-z0-9]{20}'`. A key that was ever
      committed must be rotated — deleting the file does not remove it.
- [ ] `docker compose up --build` works from a clean clone with only
      `.env.api.example` copied and filled.
- [ ] `GET /health` shows the expected `run_root` with
      `run_root_writable: true`.
- [ ] A dry-run submission (`live` omitted) reaches `succeeded`. This proves
      the whole path without spending anything.
- [ ] The hoster generated their own `DRA_API_KEY` and holds their own
      `OPENROUTER_API_KEY`. Send secrets out-of-band — a password manager or
      one-time secret link, not email or Slack.

Send them section 2 explicitly. The code-execution behaviour is what changes
how they must deploy, and it is not obvious from reading the endpoints.
