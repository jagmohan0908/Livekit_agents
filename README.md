# Vobiz LiveKit Gemini Worker

Long-running LiveKit voice worker for Vobiz AI. The worker connects outbound to
LiveKit, registers an agent name, receives calls from LiveKit dispatch rules,
then fetches the correct prompt/config from Frappe at call start.

This is a background worker, not a public HTTP service. It does not need a
public URL or inbound port.

## Production Architecture

Recommended production layout:

```text
1 Vobiz account
1 LiveKit account
1 LiveKit project per company
1 worker per company/project
many Frappe voice profiles per company
```

Example:

```text
LiveKit Project: Sriaas
  Worker: sriaas-vobiz-gemini-live
  Frappe: https://sriaas.example.com

LiveKit Project: Bharat Homeopathy
  Worker: bharat-vobiz-gemini-live
  Frappe: https://bharat.example.com

LiveKit Project: Eternity E-Commerce
  Worker: eternity-vobiz-gemini-live
  Frappe: https://eternity.example.com
```

Each company can have many agents/profiles in Frappe, for example kidney,
paralysis, male fertility, support, sales, etc. Do not create one worker per
profile. Create one worker per company and route profiles by dispatch metadata.

## Call Flow

```text
Caller
  -> Vobiz/SIP provider
  -> LiveKit inbound trunk
  -> LiveKit dispatch rule
  -> company worker, e.g. bharat-vobiz-gemini-live
  -> Frappe config endpoint
  -> Google Vertex/Gemini Live
  -> LiveKit audio back to caller
```

The dispatch rule metadata tells the worker which Frappe site/profile to load:

```json
{
  "company_key": "bharat-homeopathy",
  "frappe_base_url": "https://bharat.example.com",
  "voice_agent_profile": "atul-male-infertility",
  "profile_key": "atul-male-infertility",
  "did_number": "+917971442066"
}
```

## Worker Registration

When the worker starts, it connects outbound to LiveKit and appears in:

```text
LiveKit Cloud -> Agents
```

No manual LiveKit Agent Builder setup is required. Frappe is the source of truth
for prompts, greeting, model, voice, guardrails, and profile routing.

## Live Call Actions

The worker can call a secure Frappe action endpoint during a live call:

```text
/api/method/vobiz_ai.api.voice_actions.perform_voice_action
```

It uses the same shared header as config loading:

```text
X-Voice-Agent-Secret: VOICE_AGENT_CONFIG_SECRET
```

Supported actions:

```text
send_whatsapp
book_appointment_request
arrange_doctor_callback
create_issue
```

Enable actions per profile in Frappe:

```text
Vobiz Voice Agent Profile -> Allowed Voice Actions
```

Example:

```text
send_whatsapp,book_appointment_request,arrange_doctor_callback,create_issue
```

The worker only exposes actions returned by Frappe config, and Frappe rejects
any action not allowed on that voice profile.

## Render Deployment

Create one Render Background Worker per company from this repository.

Recommended service names:

```text
sriaas-vobiz-gemini-live
bharat-vobiz-gemini-live
eternity-vobiz-gemini-live
```

Runtime:

```text
Docker
```

Start command is already defined in the Dockerfile:

```bash
python gemini_live_agent.py start
```

Use an always-on paid worker. A sleeping/free instance is not suitable for
production calls.

## Required Environment Variables

Set these in the worker service. Do not commit real secrets.

```bash
LIVEKIT_URL=wss://company-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
LIVEKIT_AGENT_NAME=bharat-vobiz-gemini-live

FRAPPE_BASE_URL=https://company-frappe.example.com
VOICE_AGENT_CONFIG_SECRET=...

GOOGLE_APPLICATION_CREDENTIALS_JSON='{"type":"service_account", ...}'
GOOGLE_CLOUD_PROJECT=newapp-496411
VERTEX_LOCATION=us-central1

GEMINI_LIVE_MODEL=gemini-live-2.5-flash-native-audio
GEMINI_LIVE_VOICE=Puck
```

Optional:

```bash
MCP_SERVER_URL=
MCP_BEARER_TOKEN=
```

`GOOGLE_APPLICATION_CREDENTIALS_JSON` should contain the full Google service
account JSON as a single environment variable. On startup the worker writes it
to `/tmp/google-credentials.json`.

## Frappe Setup Per Company

In the company Frappe site:

```text
Vobiz AI Settings
```

Set:

```text
Company Key = bharat-homeopathy
Public Frappe Base URL = https://company-frappe.example.com
Voice Agent Config Secret = same value as worker VOICE_AGENT_CONFIG_SECRET
LiveKit URL = company LiveKit project URL
LiveKit API Key = company LiveKit API key
LiveKit API Secret = company LiveKit API secret
LiveKit Agent Dispatch Name = bharat-vobiz-gemini-live
```

For each phone number/profile:

```text
Vobiz Voice Agent Profile
```

Set:

```text
Profile Key = atul-male-infertility
Agent Name = Atul
DID / Phone Number = +917971442066
LiveKit Inbound Trunk ID = ST_xxxxx
LiveKit Agent Dispatch Name = bharat-vobiz-gemini-live
Auto Sync to LiveKit on Save = enabled
```

Then click:

```text
Deploy / Sync to LiveKit
```

The LiveKit dispatch rule should show:

```text
Agent: bharat-vobiz-gemini-live
Inbound Routing: ST_xxxxx
```

## LiveKit Setup Per Company

For each company project:

1. Create a LiveKit project.
2. Create API key/secret.
3. Create inbound SIP trunks for company phone numbers.
4. Copy each trunk ID, e.g. `ST_xxxxx`.
5. Put the LiveKit credentials in that company Frappe settings.
6. Put the same LiveKit credentials in that company worker env.
7. Sync routes from Frappe.

## AWS Deployment

The worker can run on AWS instead of Render. It still does not need a public URL.

Recommended:

```text
ECS Fargate service
```

Simple low-cost alternative:

```text
EC2 + Docker + systemd/supervisor
```

Required networking:

```text
Outbound internet to LiveKit Cloud
Outbound internet to Frappe public URL
Outbound internet to Google APIs
Inbound public HTTP: not required
```

## Local Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local
python gemini_live_agent.py console
```

Production/worker mode:

```bash
python gemini_live_agent.py start
```

## Troubleshooting

Worker not visible in LiveKit Agents:

```text
Check LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, LIVEKIT_AGENT_NAME.
Check worker logs for startup crash.
```

Call rings then cuts:

```text
Check that the LiveKit dispatch rule uses the real inbound trunk ID.
Check the dispatch rule agent name matches LIVEKIT_AGENT_NAME.
Check LiveKit Sessions and worker logs.
```

Agent says config could not load:

```text
Check FRAPPE_BASE_URL is public/reachable.
Check VOICE_AGENT_CONFIG_SECRET matches Frappe.
Check dispatch metadata contains frappe_base_url and profile_key.
```

After changing worker env/secrets:

```text
Redeploy/restart the worker.
```
