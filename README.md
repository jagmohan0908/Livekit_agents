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

## LiveKit Hosted Agent Deployment

Create one LiveKit Cloud hosted agent for the Sriaas project from this
repository. LiveKit hosts the worker process and stores the secrets. Do not use
Render for this setup.

The hosted agent should register this runtime dispatch name:

```text
sriaas-vobiz-gemini-live
```

That is the same agent name every Frappe dispatch rule should use.

Use this single LiveKit hosted agent for all Sriaas Frappe voice profiles:

```text
kamal-male-infertility -> DID/trunk/dispatch rule -> sriaas-vobiz-gemini-live
chirag-skin            -> DID/trunk/dispatch rule -> sriaas-vobiz-gemini-live
new-profile            -> DID/trunk/dispatch rule -> sriaas-vobiz-gemini-live
```

The repo includes `livekit.toml` for the Sriaas project:

```toml
[project]
  subdomain = "sriaas-new-wxe0zawn"

[agent]
  id = "CA_xxxxx"
```

After creating the hosted agent, update the `id` in `livekit.toml` to the new
`CA_...` value.

Deploy/update the hosted agent from this repo:

```bash
lk agent deploy
```

Check status:

```bash
lk agent status
```

## Required LiveKit Agent Secrets

Set these as LiveKit agent secrets. Do not commit real secrets.

```bash
LIVEKIT_URL=wss://sriaas-new-wxe0zawn.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
LIVEKIT_AGENT_NAME=sriaas-vobiz-gemini-live

FRAPPE_BASE_URL=http://test-sr.butest.tech
VOICE_AGENT_CONFIG_SECRET=...
X_VOICE_AGENT_SECRET=...

GOOGLE_APPLICATION_CREDENTIALS_JSON='{"type":"service_account", ...}'
GOOGLE_CLOUD_PROJECT=newapp-496411
VERTEX_LOCATION=us-central1

GEMINI_LIVE_MODEL=gemini-live-2.5-flash-native-audio
GEMINI_LIVE_VOICE=Puck
```

For Google credentials, add a LiveKit file secret named `creds.json`, then set:

```bash
GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/creds.json
```

Optional:

```bash
MCP_SERVER_URL=
MCP_BEARER_TOKEN=
```

`GOOGLE_APPLICATION_CREDENTIALS_JSON` is also supported for non-LiveKit hosts,
but LiveKit file secret `creds.json` is preferred here.

## Frappe Setup Per Company

In the company Frappe site:

```text
Vobiz AI Settings
```

Set:

```text
Company Key = sriaas
Public Frappe Base URL = http://test-sr.butest.tech
Voice Agent Config Secret = same value as worker VOICE_AGENT_CONFIG_SECRET
LiveKit URL = wss://sriaas-new-wxe0zawn.livekit.cloud
LiveKit API Key = company LiveKit API key
LiveKit API Secret = company LiveKit API secret
LiveKit Agent Dispatch Name = sriaas-vobiz-gemini-live
```

For each phone number/profile:

```text
Vobiz Voice Agent Profile
```

Set:

```text
Profile Key = kamal-male-infertility
Agent Name = Kamal
DID / Phone Number = +919262102420
LiveKit Inbound Trunk ID = ST_xxxxx
LiveKit Agent Dispatch Name = sriaas-vobiz-gemini-live
Auto Sync to LiveKit on Save = enabled
```

Then click:

```text
Deploy / Sync to LiveKit
```

The LiveKit dispatch rule should show:

```text
Agent: sriaas-vobiz-gemini-live
Inbound Routing: ST_xxxxx
```

For a second Frappe profile, keep the same LiveKit Agent Dispatch Name and only
change the profile key, DID, trunk ID, prompt, and optional account mapping.

## LiveKit Setup Per Company

For each company project:

1. Create a LiveKit project.
2. Create API key/secret.
3. Create inbound SIP trunks for company phone numbers.
4. Copy each trunk ID, e.g. `ST_xxxxx`.
5. Put the LiveKit credentials in that company Frappe settings.
6. Put the same LiveKit credentials in that company worker env.
7. Sync routes from Frappe.

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
