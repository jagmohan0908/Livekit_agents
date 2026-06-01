# LiveKit Gemini Voice Agents for Render

This repo runs SRIAAS Gemini Live voice workers on Render.

Each Render worker is a separate LiveKit agent dispatch name, for example:

- `kamal-male-infertility-agent`
- `chirag-skin-agent`

Frappe remains the source of truth for the agent name, prompt, voice settings,
phone number, trunk, and dispatch rule metadata.

Simple production flow:

```text
Customer phone call
 -> Vobiz SIP trunk
 -> LiveKit Cloud room
 -> the matching Render worker running gemini_live_agent.py
 -> Gemini Live on Vertex AI
 -> MCP/Frappe lead creation
```

## Deploy on Render

Use Render Blueprint with `render.yaml`, or create Background Workers manually.
The included blueprint creates two workers:

- `kamal-male-infertility-agent`
- `chirag-skin-agent`

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
LIVEKIT_AGENT_NAME=kamal-male-infertility-agent
FRAPPE_BASE_URL=https://your-frappe-site.example.com
VOICE_AGENT_CONFIG_SECRET=...
GOOGLE_CLOUD_PROJECT=...
GOOGLE_APPLICATION_CREDENTIALS_JSON='{"type":"service_account", ...}'
VERTEX_LOCATION=us-central1
GEMINI_LIVE_MODEL=gemini-live-2.5-flash-native-audio
GEMINI_LIVE_VOICE=Puck
MCP_SERVER_URL=https://your-mcp-server.example.com/mcp
MCP_BEARER_TOKEN=...
```

For another worker, keep the same secrets but change:

```bash
LIVEKIT_AGENT_NAME=chirag-skin-agent
```

`GOOGLE_APPLICATION_CREDENTIALS_JSON` should contain the full Google service account JSON. The app writes it to `/tmp/google-credentials.json` on startup.

## Frappe Setup

For each `/app/vobiz-voice-agent-profile/...`:

- Set `LiveKit Agent Dispatch Name` to the matching Render worker name.
- Example for Kamal: `kamal-male-infertility-agent`
- Example for Chirag: `chirag-skin-agent`
- Fill DID and LiveKit Inbound Trunk ID.
- Click `Deploy / Sync to LiveKit`.

Frappe creates/updates the LiveKit SIP dispatch rule. It does not need to create
a LiveKit Cloud Agent when workers are hosted on Render.

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
