# Gemini Live Agent for LiveKit SIP Calls

This repo runs the SRIAAS lead-generation voice agent named Kamal.

Simple production flow:

```text
Customer phone call
 -> Vobiz SIP trunk
 -> LiveKit Cloud room
 -> this Render worker running gemini_live_agent.py
 -> Gemini Live on Vertex AI
 -> MCP/Frappe lead creation
```

## Deploy on Render

Use Render Blueprint with `render.yaml`, or create a Background Worker manually.

Build/runtime:

- Runtime: Docker
- Start command is inside the Dockerfile:

```bash
python gemini_live_agent.py start
```

Use an always-on paid worker. A sleeping/free instance is not suitable for calls.

## Required Environment Variables

Set these in Render:

```bash
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
GOOGLE_CLOUD_PROJECT=...
GOOGLE_APPLICATION_CREDENTIALS_JSON='{"type":"service_account", ...}'
VERTEX_LOCATION=us-central1
GEMINI_LIVE_MODEL=gemini-live-2.5-flash-native-audio
GEMINI_LIVE_VOICE=Puck
MCP_SERVER_URL=https://your-mcp-server.example.com/mcp
MCP_BEARER_TOKEN=...
```

`GOOGLE_APPLICATION_CREDENTIALS_JSON` should contain the full Google service account JSON. The app writes it to `/tmp/google-credentials.json` on startup.

## Local Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local
python gemini_live_agent.py console
```

For production/Render:

```bash
python gemini_live_agent.py start
```
