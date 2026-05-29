# Runbook — Start / Stop the App

Copy-paste commands to run ResearchFlow AI yourself. Windows + Docker Desktop.
The whole stack (Postgres, Chroma, FastAPI backend, Next.js frontend) runs via
Docker Compose — you do not need Python or Node installed locally to *run* it.

| Service  | URL                                | Port |
| -------- | ---------------------------------- | ---- |
| Frontend | http://localhost:3000              | 3000 |
| Backend  | http://localhost:8000              | 8000 |
| API docs | http://localhost:8000/docs         | 8000 |
| Health   | http://localhost:8000/api/v1/health| 8000 |
| Postgres | localhost:5433                     | 5433 |
| Chroma   | http://localhost:8001              | 8001 |

> All commands below are **PowerShell** and assume you are in the project root:
> `cd C:\Users\Karthi\Desktop\Research-Automation-with-AI`
>
> **Your setup:** Docker Desktop is always on and the four containers already
> exist. So your normal flow is just **start the existing containers** — no
> build needed. Use §0.

---

## 0. Your normal flow — start the app

Docker Desktop already running + containers already built. Just start them:

```powershell
docker compose start
```

That's it — open http://localhost:3000. To confirm everything is up:

```powershell
docker compose ps
curl http://localhost:8000/api/v1/health
```

Expected health: `{"status":"ok","version":"0.1.0"}`. All four services should
read `Up` (postgres also shows `(healthy)`).

> `docker compose start` resumes the existing stopped containers (fast).
> Use §1 only after pulling new code or changing dependencies.

## 1. After code / dependency changes — rebuild + start

Only needed when the backend deps or a Dockerfile changed:

```powershell
docker compose up -d --build
```

`-d` runs detached; `--build` rebuilds the images. If you only edited source
files (backend has `--reload`, frontend has hot reload via volume mounts), you
don't need this — `docker compose start` (§0) is enough.

## 4. Watch logs

```powershell
# all services, follow
docker compose logs -f

# just the backend, last 50 lines
docker compose logs backend --tail 50

# just the frontend
docker compose logs frontend -f
```

## 5. If Docker Desktop isn't running (rare — you keep it on)

You normally leave Docker Desktop on, so you can skip this. But if a
`docker compose` command errors with
`failed to connect to the docker API ... dockerDesktopLinuxEngine`, the engine
is down — start it and wait for it to be ready:

```powershell
Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
do { Start-Sleep 3 } until (docker info 2>$null); "Docker engine ready"
```

Then run §0.

---

## Stop / restart / clean

```powershell
# stop containers but keep data (Postgres + Chroma volumes survive)
docker compose stop

# stop and remove containers + network (volumes survive)
docker compose down

# restart a single service (e.g. after editing backend code that didn't auto-reload)
docker compose restart backend

# nuke EVERYTHING including the database + vector store (destructive — fresh start)
docker compose down -v
```

> `down -v` deletes the `postgres_data` and `chroma_data` volumes. Only use it
> when you want a clean database. It is irreversible.

---

## Running tests / lint (optional, needs the backend venv)

These run on the host, not in Docker. The backend venv lives at
`backend\.venv` (Python 3.13). From the `backend` folder:

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q                       # full test suite
.\.venv\Scripts\python.exe -m ruff check app/ tests/          # lint
.\.venv\Scripts\python.exe -m ruff format --check app/ tests/ # format check
.\.venv\Scripts\python.exe -m mypy --strict app/              # type check
cd ..
```

> Note: the Docker image runs **Python 3.11**; the host venv is **3.13**. A few
> 3.11-only issues (e.g. Pydantic requiring `typing_extensions.TypedDict` for
> response models) won't surface in the host venv — always confirm a clean
> `docker compose up --build` boot after touching response-model types.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `failed to connect to the docker API` | Docker Desktop not running — see §5. |
| Port already in use (3000/8000/5433/8001) | Something else is bound to it. `docker compose down`, or stop the other process. |
| Backend keeps restarting | `docker compose logs backend --tail 80` — look for a Python traceback at the bottom. |
| Frontend 500 / blank | `docker compose logs frontend -f` — usually a missing `frontend\.env.local` var. |
| Code change not reflected | Backend: `docker compose restart backend`. Frontend: hard-refresh the browser; if stuck, `docker compose restart frontend`. |
| Need a clean DB | `docker compose down -v` then §1. |

---

## TL;DR

```powershell
# YOUR NORMAL FLOW — start existing containers   -> http://localhost:3000
docker compose start

# only after code/dependency changes (rebuild)
docker compose up -d --build

# status + health
docker compose ps
curl http://localhost:8000/api/v1/health

# logs
docker compose logs -f

# stop (keep data)
docker compose down
```
