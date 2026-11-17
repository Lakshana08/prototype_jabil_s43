# prototype_jabil_s43 - Smart Material AI Chat

Two separate apps, each run and deployed on its own:

| Folder | What | Deploy |
|---|---|---|
| `backend/` | Python FastAPI Planner agent (A2A at `/a2a/planner`), reads live SAP S43 OData V4. Settings in `backend/.env` (copy from `backend/.env.example`, never commit it). | `cd backend && cf push` |
| `frontend/` | Static chat UI (HTML/CSS/JS). Backend URL in `frontend/config.js`. | `cd frontend && cf push` |

`Spec/` holds the functional and technical specifications.

## Run locally

```bash
# terminal 1 - backend
cd backend
python -m uvicorn app.main:app --port 8000 --reload

# terminal 2 - frontend
cd frontend
python -m http.server 5500
```

Open http://localhost:5500.
