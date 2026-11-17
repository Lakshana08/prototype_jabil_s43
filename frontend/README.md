# Smart Material AI Chat - frontend

Static chat UI (HTML/CSS/JS, no build step) for the Planner agent. It is deployed on its own; the
backend is the separate Python app in `../backend`, called over HTTP:

- `POST {apiBase}/a2a/planner` - A2A 1.0 JSON-RPC (`SendMessage`)
- `GET  {apiBase}/a2a/planner/.well-known/agent-card.json`, `GET {apiBase}/health`

## Files

| File | Purpose |
|---|---|
| `index.html`, `app.js`, `styles.css` | The UI |
| `config.js` | Backend URL: local backend on localhost, deployed backend everywhere else |
| `manifest.yml`, `Staticfile`, `.cfignore` | Cloud Foundry deployment (staticfile buildpack) |

## Run locally

1. Start the backend (in `backend/`): `python -m uvicorn app.main:app --port 8000`
2. In `frontend/`: `python -m http.server 5500`
3. Open http://localhost:5500

## Deploy to SAP BTP Cloud Foundry

1. Deploy the backend first (`cf push` in `backend/`) and note its route (`cf app smart-material-chat-s43`).
2. If the backend route changed, update `DEPLOYED_BACKEND` in `config.js`.
3. In `frontend/`: `cf push`
4. Open the UI route shown by `cf push`.

`?api=https://<host>` in the browser URL overrides `config.js` (handy for testing another backend).
The backend allows cross-origin requests (CORS `*`), so UI and backend can run on different hosts.
