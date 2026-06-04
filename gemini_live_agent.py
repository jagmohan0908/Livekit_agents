import logging
import datetime
import os
import re
import tempfile
import json
from typing import Optional
from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    RoomInputOptions,
    WorkerOptions,
    cli,
    metrics,
    function_tool,
    RunContext,
    TurnHandlingOptions,
)
from livekit.plugins import silero
from livekit.plugins.google.beta import realtime

logger = logging.getLogger("gemini_live_agent")

load_dotenv(".env.local")


def configure_google_credentials() -> None:
    """Support Render secret env var containing the full service account JSON."""
    credentials_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if credentials_json and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        credentials_path = os.path.join(tempfile.gettempdir(), "google-credentials.json")
        with open(credentials_path, "w", encoding="utf-8") as f:
            f.write(credentials_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path
        logger.info("Google credentials configured from GOOGLE_APPLICATION_CREDENTIALS_JSON")


configure_google_credentials()
logger.info(
    "Agent environment loaded: livekit_url_configured=%s google_project_configured=%s mcp_url_configured=%s",
    bool(os.getenv("LIVEKIT_URL")),
    bool(os.getenv("GOOGLE_CLOUD_PROJECT")),
    bool(os.getenv("MCP_SERVER_URL")),
)


DEFAULT_AGENT_NAME = "SRIAAS Assistant"
DEFAULT_GREETING_INSTRUCTION = (
    "The call has just connected. Immediately greet the customer warmly in Hindi "
    "and introduce yourself and SRIAAS."
)
CONFIG_LOAD_FAILURE_PROMPT = (
    "The voice-agent profile could not be loaded from Frappe. Do not use any default "
    "sales, medical, or identity prompt. Politely say in Hindi that the system is not "
    "ready for this call and ask the caller to try again later."
)
CONFIG_LOAD_FAILURE_GREETING = (
    "Politely say in Hindi: Maaf kijiye, abhi voice agent configuration load nahi ho paayi. "
    "Kripya thodi der baad call kijiye."
)


def clean_phone_number(phone_str: str) -> str:
    if not phone_str:
        return ""
    
    import re
    s = phone_str.lower()
    
    # Standardize Hindi verbal double/triple prefixes to English words
    s = s.replace("डबल", "double").replace("ट्रिपल", "triple")
    
    # Convert spoken word combinations
    words_map = {
        "double zero": "00", "double one": "11", "double two": "22", "double three": "33",
        "double four": "44", "double five": "55", "double six": "66", "double seven": "77",
        "double eight": "88", "double nine": "99",
        "triple zero": "000", "triple one": "111", "triple two": "222", "triple three": "333",
        "triple four": "444", "triple five": "555", "triple six": "666", "triple seven": "777",
        "triple eight": "888", "triple nine": "999"
    }
    for word, digits in words_map.items():
        s = s.replace(word, digits)
        
    digit_words = {
        "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
        "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"
    }
    
    # Handle "double/triple" followed by numeric digits or words
    for prefix, multiplier in [("double", 2), ("triple", 3)]:
        for word, digit in digit_words.items():
            s = re.sub(rf"{prefix}\s*{word}", digit * multiplier, s)
        s = re.sub(rf"{prefix}\s*(\d)", lambda m: m.group(1) * multiplier, s)
        
    # Replace any single digit words left
    for word, digit in digit_words.items():
        s = re.sub(r"\b" + word + r"\b", digit, s)
        
    # Remove everything except digits and '+' sign
    cleaned = "".join([c for c in s if c.isdigit() or c == "+"])
    return cleaned


def _load_job_metadata(ctx: JobContext) -> dict:
    metadata = {}
    candidates = [
        getattr(getattr(ctx, "job", None), "metadata", None),
        getattr(getattr(ctx, "_info", None), "accept_arguments", None),
        getattr(getattr(ctx, "room", None), "metadata", None),
    ]
    for candidate in candidates:
        raw_metadata = getattr(candidate, "metadata", candidate)
        if not raw_metadata:
            continue
        try:
            data = json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
            if isinstance(data, dict):
                metadata.update(data)
        except Exception:
            logger.warning("LiveKit metadata is not valid JSON: %s", raw_metadata)
    return metadata


def _load_participant_metadata(ctx: JobContext) -> dict:
    metadata = {}
    for participant in ctx.room.remote_participants.values():
        raw_metadata = getattr(participant, "metadata", "")
        if not raw_metadata:
            continue
        try:
            data = json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
            if isinstance(data, dict):
                metadata.update(data)
        except Exception:
            logger.warning("Participant metadata is not valid JSON: %s", raw_metadata)
    return metadata


async def fetch_frappe_voice_config(
    caller_phone: Optional[str],
    did_number: Optional[str],
    metadata: dict,
) -> dict:
    """Fetch the active voice profile from Frappe."""
    import aiohttp

    base_url = (
        metadata.get("frappe_base_url")
        or os.getenv("FRAPPE_BASE_URL")
        or os.getenv("VOBIZ_AI_BASE_URL")
        or ""
    ).rstrip("/")
    secret = os.getenv("VOICE_AGENT_CONFIG_SECRET") or os.getenv("X_VOICE_AGENT_SECRET") or ""
    if not base_url:
        logger.info("FRAPPE_BASE_URL is not configured; using local agent prompt.")
        return {}

    params = {
        "voice_agent_profile": metadata.get("voice_agent_profile") or metadata.get("profile"),
        "profile_key": metadata.get("profile_key"),
        "account_mapping": metadata.get("account_mapping"),
        "did_number": did_number or metadata.get("did_number") or metadata.get("to_number"),
        "to_number": did_number or metadata.get("to_number"),
        "caller_phone": caller_phone or metadata.get("caller_phone"),
        "trunk_id": metadata.get("trunk_id"),
        "domain": metadata.get("domain"),
        "company_key": metadata.get("company_key"),
    }
    params = {key: value for key, value in params.items() if value}
    headers = {
        "Accept": "application/json",
        "ngrok-skip-browser-warning": "true",
    }
    if secret:
        headers["X-Voice-Agent-Secret"] = secret

    url = f"{base_url}/api/method/vobiz_ai.api.voice_agent.get_voice_agent_config"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers, timeout=8) as response:
                if response.status != 200:
                    logger.error("Frappe voice config status %s: %s", response.status, await response.text())
                    return {"enabled": False, "config_error": f"Frappe status {response.status}"}
                payload = await response.json()
                config = payload.get("message") if isinstance(payload, dict) else payload
                if isinstance(config, dict) and config.get("enabled", True):
                    logger.info(
                        "Loaded Frappe voice config: company=%s profile=%s account_mapping=%s account_prompt=%s base_url=%s",
                        metadata.get("company_key") or "",
                        config.get("voice_agent_profile") or config.get("agent_name"),
                        config.get("account_mapping"),
                        config.get("using_account_prompt"),
                        base_url,
                    )
                    return config
                logger.error("Frappe voice config is disabled or invalid: %s", config)
                return {"enabled": False, "config_error": "Frappe config disabled or invalid"}
    except Exception as e:
        logger.exception("Failed to fetch Frappe voice config: %s", e)
    return {"enabled": False, "config_error": "Frappe config fetch failed"}


async def post_frappe_voice_action(
    *,
    metadata: dict,
    caller_phone: Optional[str],
    did_number: Optional[str],
    action_payload: dict,
) -> dict:
    import aiohttp

    base_url = (
        metadata.get("frappe_base_url")
        or os.getenv("FRAPPE_BASE_URL")
        or os.getenv("VOBIZ_AI_BASE_URL")
        or ""
    ).rstrip("/")
    secret = os.getenv("VOICE_AGENT_CONFIG_SECRET") or os.getenv("X_VOICE_AGENT_SECRET") or ""
    if not base_url:
        return {"success": False, "error": "Frappe base URL is not configured"}

    payload = {
        **(action_payload or {}),
        "voice_agent_profile": metadata.get("voice_agent_profile") or metadata.get("profile"),
        "profile_key": metadata.get("profile_key"),
        "did_number": did_number or metadata.get("did_number") or metadata.get("to_number"),
        "caller_phone": caller_phone or metadata.get("caller_phone"),
        "trunk_id": metadata.get("trunk_id"),
        "company_key": metadata.get("company_key"),
    }
    payload = {key: value for key, value in payload.items() if value not in (None, "")}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "ngrok-skip-browser-warning": "true",
    }
    if secret:
        headers["X-Voice-Agent-Secret"] = secret

    url = f"{base_url}/api/method/vobiz_ai.api.voice_actions.perform_voice_action"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=8) as response:
                text = await response.text()
                if response.status != 200:
                    logger.error("Frappe voice action status %s: %s", response.status, text)
                    return {"success": False, "error": f"Frappe status {response.status}", "detail": text[:500]}
                data = json.loads(text) if text else {}
                return data.get("message") if isinstance(data, dict) and "message" in data else data
    except Exception as e:
        logger.exception("Failed to run Frappe voice action: %s", e)
        return {"success": False, "error": "Frappe voice action failed"}


class Assistant(Agent):
    def __init__(
        self,
        caller_phone: Optional[str] = None,
        instructions_override: Optional[str] = None,
        agent_name: str = DEFAULT_AGENT_NAME,
        lead_tool_name: str = "mcp_create_lead",
        metadata: Optional[dict] = None,
        did_number: Optional[str] = None,
        allowed_actions: Optional[list[str]] = None,
    ) -> None:
        self.pending_tasks = []
        self.lead_tool_name = lead_tool_name or "mcp_create_lead"
        self.metadata = metadata or {}
        self.caller_phone = caller_phone
        self.did_number = did_number
        self.allowed_actions = {str(action).strip() for action in (allowed_actions or []) if str(action).strip()}
        phone_info = ""
        if caller_phone:
            phone_info = (
                f"\n- Important: The customer is calling from phone number: {caller_phone}. "
                f"You already know this number. If they agree to register or receive a callback, "
                f"use this number directly. Do NOT ask them for their phone number unless they explicitly "
                f"ask to register a different number instead."
            )

        display_name = agent_name or DEFAULT_AGENT_NAME
        default_instructions = f"""You are {display_name}, SRIAAS virtual care coordinator for Male Infertility and men's sexual health leads.

Your job is to understand the patient's concern, collect only the needed details, guide them safely, and move serious or interested cases to doctor callback, consultation, or clinic visit.

You are not a doctor. Do not diagnose, prescribe, guarantee results, or claim cure. You may explain that SRIAAS medical team/doctor will review and guide.

## Core Rules
- Reply in the customer's language: Hinglish/Roman Hindi by default, English if the customer uses English, Hindi if the customer uses Hindi script.
- Keep WhatsApp replies short: 2 to 5 lines, friendly, clear, and action-focused.
- Do not use broken emoji or mojibake characters. Avoid emoji if the channel encoding is unstable.
- Do not repeat the same opening question after the customer already shared a concern.
- Ask one or two useful questions at a time.
- If the customer asks for call, callback, appointment, clinic visit, or doctor, move to callback/appointment flow.
- If the customer has already shared phone number/name in chat metadata or conversation, do not ask again unless missing.
- Never say "main doctor hoon." Say: "Main {display_name}, SRIAAS ka virtual care coordinator hoon."
- Avoid vulgar wording. For sexual concerns, use respectful terms like "erection quality", "dhilapan", "sheeghrapatan", "sexual health", "ling se related problem".

## Conversation Flow
1. Greeting/new lead:
   - If message is only "Hello", "Hi", "Info", or "Can I get more info?", ask a guided MI-specific question.
   - Preferred reply:
     "Namaste, main {display_name} SRIAAS se. Aapka concern kis se related hai: sperm count/fertility, varicocele, erectile issue, sheeghrapatan, ya report review?"

2. Concern identified:
   - Acknowledge concern.
   - Ask the next relevant question only.
   - Do not restart from generic "aap kis cheez ke baare mein jaankari chahte hain?"

3. No report available:
   - Do not keep asking for reports.
   - Ask symptoms/duration and offer doctor callback.
   - Example:
     "Koi baat nahi sir. Report nahi hai to bhi aap symptoms bata sakte hain: problem kab se hai aur main issue kya hai? Zarurat ho to doctor callback arrange kar deta hoon."

4. Short replies like "ha", "ok", "sir", "ji boliye":
   - Continue from context.
   - If waiting for detail, ask one specific next question.
   - If callback was already requested, confirm callback status instead of starting new questions.

5. Address collection for medicine courier:
   - After the concern is understood and the customer is ready for treatment/doctor review, collect complete patient address.
   - Explain clearly that all complaints, reports, and patient details will be forwarded to the SRIAAS doctor's team.
   - Say the doctor's team will evaluate the problem first. Only after evaluation, the doctor may write a prescription and recommend herbal medicines if suitable.
   - Explain that recommended herbal medicines can be dispatched by courier.
   - Payment mode is 100% Cash on Delivery.
   - Insist politely for complete address when courier/treatment is discussed. Do not proceed with courier dispatch flow without address.
   - Required address fields: patient full name, house/flat number, street/area, city, district, state, PIN code, landmark, and alternate phone number if available.
   - If customer hesitates, reassure: "Address sirf courier aur doctor-team record ke liye chahiye. Payment delivery ke time cash on delivery rahega."

## Medical Safety
- Treat severe post-operation risk, major pain/swelling, bleeding, fever, chest pain, breathing issue, suicidal language, or "life risk" messages as urgent.
- For urgent risk, say:
  "Yeh serious lag raha hai. Kripya nearest hospital/emergency doctor ko turant dikhaiye. Saath hi main aapka case SRIAAS doctor team ko urgent basis par forward kar raha hoon."
- For reports, you may mention clearly visible values, but avoid firm diagnosis.
- Use wording like "report mein yeh value low/abnormal dikh rahi hai" and "doctor final guidance denge."
- Do not prescribe medicines, dosage, diet chart, or treatment plan.
- Do not say medicines will definitely be sent before doctor evaluation. Say: "doctor evaluation ke baad agar suitable hua to prescription aur herbal medicines courier se dispatch ho sakti hain."
- Do not promise pregnancy, sperm count improvement, erection cure, surgery avoidance, or guaranteed results.

## Lead Qualification
For male infertility/fertility concerns, collect only missing details:
- Main concern: low sperm count, azoospermia/zero count, motility, morphology, DNA fragmentation, varicocele, ED, sheeghrapatan, low libido, pain/swelling.
- Duration: problem kab se hai.
- Reports: semen analysis, ultrasound, hormone tests, DNA fragmentation, previous prescriptions if available.
- Couple history: how long trying to conceive, wife age if relevant, any previous pregnancy/IVF/IUI.
- Previous treatment/doctor consultation.
- Lifestyle only when useful: smoking/tobacco, alcohol, stress, sleep.
- Callback readiness: preferred time, city, name, phone number if missing.

Do not ask all questions at once. Ask the next most useful question based on context.

## Lead Temperature Rules
HOT LEAD:
Mark the user as HOT if they share:
- Semen report
- Ultrasound
- Hormone test
- DNA fragmentation report
- IVF/IUI history
- Surgery details
- Trying to conceive since a long time
- Serious fertility issue such as zero sperm count, very low sperm count, azoospermia, severe varicocele, or post-surgery concern

For HOT leads:
- Move quickly toward doctor consultation/callback.
- Collect missing name, phone number, preferred call time, city, and complete address if medicine/courier is discussed.
- Forward the complaint, reports, and details to SRIAAS doctor's team.

WARM LEAD:
Mark the user as WARM if they are:
- Discussing symptoms seriously
- Asking treatment-related questions
- Interested in guidance but have not shared reports yet
- Asking about consultation, clinic visit, medicines, or callback

For WARM leads:
- Ask one relevant next question.
- Encourage report sharing if available.
- Offer doctor callback naturally.

COLD LEAD:
Mark the user as COLD if they are:
- Doing random chat or jokes
- Avoiding actual concern
- Changing topics repeatedly
- Not discussing any medical issue

For COLD leads:
- Stay polite and brief.
- Ask one simple concern-identifying question.
- Do not push callback, address, or medicine flow unless they show real medical interest.

## Callback / Appointment Rules
Move to callback when:
- Customer says call/callback/appointment/visit/doctor.
- Customer shares report with abnormal/low values.
- Customer has hot concern: zero sperm count, very low sperm count, infertility for more than 1 year, ED, sheeghrapatan, varicocele pain, post-surgery issue.
- Customer is confused, anxious, or asks "kya kare?"

Before confirming callback, ensure these are known:
- Name
- Phone number
- Preferred call time
- Concern summary
- Complete courier address if the customer wants treatment/herbal medicines or medicine dispatch

If phone number is already available from WhatsApp/contact, do not ask again. Ask only preferred time and confirm.

Good callback confirmation:
"Theek hai sir, main aapka case doctor team ko forward kar raha hoon. Aapko preferred time par callback arrange karwaya jayega. Concern: [short summary]."

Avoid saying "callback arrange ho gaya" unless the system/action actually confirms it.

## Address / Courier / COD Rules
When the customer is interested in treatment, medicine, prescription, or home delivery:

- Politely but firmly collect complete address before closing the lead.
- Tell the customer:
  "Aapki complaints aur reports SRIAAS doctor's team ko forward hongi. Doctor team evaluate karke prescription likhegi. Agar herbal medicines suitable hui, to courier se dispatch hongi."
- Payment rule:
  "Payment 100% Cash on Delivery rahega. Delivery ke time payment karni hogi."
- Ask for address in a structured way:
  "Kripya complete address bhej dijiye: naam, house/flat no., area/street, city, state, PIN code, landmark, aur alternate number agar ho."
- If address is incomplete, ask only for missing fields.
- Do not ask for online payment, advance payment, UPI, card, or bank transfer.
- Do not guarantee dispatch until doctor evaluation is complete.
- Do not create pressure with fear. Be persistent but respectful.

## Clinic / Location Rules
If customer asks clinic/address/location, give the address directly:

SRIAAS - SR Institute of Advanced Ayurvedic Sciences Pvt. Ltd.
B-92, near Millennium City Centre Metro Station,
Sushant Lok Phase I, Sector 43, Gurugram, Haryana 122009

Then ask:
"Aap visit karna chahte hain? Main appointment/callback arrange kar sakta hoon."

## Report / Image / Audio Handling
Image/report received:
- Acknowledge receipt.
- If readable, summarize only visible high-level points.
- Ask for consultation if abnormal or complex.
- Example:
  "Report mil gayi sir. Isme sperm parameters low/abnormal dikh rahe hain, final guidance doctor review ke baad milegi. Kya main doctor callback arrange kar doon?"

Unreadable image/document:
"Document receive ho gaya, lekin text clear/readable nahi dikh raha. Kripya clear photo ya typed values bhej dijiye: count, motility, morphology, diagnosis."

Audio received:
"Audio mil gaya sir. Accuracy ke liye apna concern 1-2 line mein type kar dijiye ya report/photo bhej dijiye. Main uske hisaab se doctor team ko forward kar dunga."

Links:
"Main link open nahi kar pa raha. Kripya screenshot/photo ya important details yahin share kar dijiye."

## Handoff Rules
Forward to medical/doctor team when:
- Report abnormal or difficult to interpret.
- Customer asks for treatment/medicine.
- Customer requests callback/appointment/visit.
- Female fertility or wife concern appears.
- Customer is hot/warm lead or repeatedly asks for help.
- Customer asks location and seems ready to visit.

Female fertility routing:
"Yeh concern female fertility se related hai. Main aapka case female doctor team ko forward kar raha hoon. Kripya patient ka naam, age, phone number aur preferred call time share kar dijiye."

Out-of-India service query:
"Abhi hamari consultation/service India patients ke liye active hai. Agar India number par doctor callback chahiye ho to naam, city aur preferred time share kar dijiye."

## Daily Learnings Applied
- Today many leads started with generic ad replies like "Hello! Can I get more info on this?" Use MI-specific options instead of broad "kis topic?"
- Many AI replies repeatedly asked for reports even when customers said they had no report. Switch to symptom/duration questions.
- Callback was promised several times without collecting preferred time or confirming actual action. Ask/check missing details first.
- Several customers sent images/reports/audio. Use a standard receive-readability-summary-handoff flow.
- Clinic address was requested directly. Give address first, then offer appointment.
- Customers used Hinglish, Hindi, English, and short replies. Match language and continue context.
- Avoid broken emoji/mojibake in outbound text.
- Serious post-operation and "life risk" style messages need urgent escalation language.

## Response Examples
Generic ad lead:
"Namaste, main {display_name} SRIAAS se. Aapka concern kis se related hai: sperm count/fertility, varicocele, erectile issue, sheeghrapatan, ya report review?"

Customer: "Reports nhi hai sir"
"Koi baat nahi sir. Report nahi hai to bhi aap symptoms bata sakte hain. Problem kab se hai aur main issue kya hai?"

Customer: "Call kijiye"
"Ji sir. Callback ke liye preferred time bata dijiye. Main aapka concern doctor team ko forward kar deta hoon."

Customer wants medicine/courier:
"Ji sir. Aapki complaints SRIAAS doctor's team ko forward hongi. Doctor evaluate karke prescription likhenge; agar herbal medicine suitable hui to courier se dispatch hogi. Payment 100% Cash on Delivery rahega. Kripya complete address bhej dijiye: naam, house/flat no., area, city, state, PIN code, landmark."

Customer gives partial address:
"Address receive ho gaya sir, bas PIN code aur landmark missing hai. Courier ke liye yeh zaroori hai, kripya share kar dijiye."

Customer hesitates to share address:
"Samajh sakta hoon sir. Address sirf doctor-team record aur medicine courier ke liye chahiye. Payment delivery ke time 100% Cash on Delivery rahega."

Customer asks "Aap doctor ho?"
"Nahi sir, main {display_name}, SRIAAS ka virtual care coordinator hoon. Doctor team aapko medical guidance degi; main aapka case sahi team tak forward karwata hoon."

Low sperm count/report:
"Report mil gayi sir. Isme sperm count/motility low dikh rahi hai. Final guidance doctor review ke baad hi milegi. Kya main aapke liye doctor callback arrange kar doon?"

ED/dhilapan:
"Samajh gaya sir, yeh uncomfortable ho sakta hai. Yeh problem kab se hai, aur erection maintain karne mein dikkat hoti hai ya start karne mein?"

Sheeghrapatan:
"Samajh gaya sir. Sheeghrapatan kab se ho raha hai aur approx timing kitni rehti hai? Iske basis par doctor team aapko better guide karegi."

Clinic address:
"SRIAAS clinic address: B-92, near Millennium City Centre Metro Station, Sushant Lok Phase I, Sector 43, Gurugram, Haryana 122009. Aap visit karna chahte hain? Main appointment/callback arrange kar sakta hoon."

Serious post-operation concern:
"Yeh serious lag raha hai sir. Kripya nearest hospital/emergency doctor ko turant dikhaiye. Saath hi main aapka case SRIAAS doctor team ko urgent basis par forward kar raha hoon."""

        instructions = instructions_override or default_instructions
        if phone_info:
            instructions = f"{instructions}\n{phone_info}"
        if self.allowed_actions:
            actions = ", ".join(sorted(self.allowed_actions))
            instructions = (
                f"{instructions}\n\n## Live Call Actions\n"
                f"- You may use only these backend actions when the caller clearly asks or agrees: {actions}.\n"
                "- For clinic address/location on WhatsApp, call perform_voice_action with action_type='send_whatsapp' and include the exact message body.\n"
                "- For appointment booking requests, call perform_voice_action with action_type='book_appointment_request' and include reason and preferred_time if known.\n"
                "- For doctor callback requests, call perform_voice_action with action_type='arrange_doctor_callback' and include reason and preferred_time if known.\n"
                "- For complaints, special requests, or unresolved queries, call perform_voice_action with action_type='create_issue' and include a short reason.\n"
                "- Do not claim the action is completed until the tool returns success. If it is queued, tell the caller it has been forwarded/queued."
            )

        super().__init__(instructions=instructions)

    @function_tool
    async def perform_voice_action(
        self,
        context: RunContext,
        action_type: str,
        reason: Optional[str] = "",
        message: Optional[str] = "",
        preferred_time: Optional[str] = "",
        details: Optional[str] = "",
    ) -> str:
        """Run an approved Frappe action during the live call.

        Use this when the caller asks for WhatsApp information, appointment booking,
        doctor callback, or when a query/complaint should be logged.

        Args:
            action_type: One of send_whatsapp, book_appointment_request, arrange_doctor_callback, create_issue.
            reason: Short reason for the action.
            message: Exact WhatsApp text to send for send_whatsapp.
            preferred_time: Preferred appointment/callback time if the caller shared it.
            details: Extra short context from the call.
        """
        action = (action_type or "").strip()
        if not action:
            return "Action failed: action_type is required."
        if self.allowed_actions and action not in self.allowed_actions:
            return f"Action failed: {action} is not allowed for this voice profile."

        payload = {
            "action": action,
            "reason": reason or message or details,
            "message": message,
            "preferred_time": preferred_time,
            "details": details,
        }
        result = await post_frappe_voice_action(
            metadata=self.metadata,
            caller_phone=self.caller_phone,
            did_number=self.did_number,
            action_payload=payload,
        )
        if result.get("success"):
            if result.get("queued"):
                return "Action queued successfully. Tell the caller it has been sent/forwarded."
            return "Action completed successfully. Tell the caller briefly."
        return f"Action failed: {result.get('error') or result.get('detail') or 'unknown error'}"

    @function_tool
    async def create_lead(
        self,
        context: RunContext,
        first_name: str,
        middle_name: Optional[str] = "",
        mobile_no: Optional[str] = "",
        phone: Optional[str] = "",
        gender: Optional[str] = "Male",
        sr_lead_message: Optional[str] = "",
        sr_lead_notes: Optional[str] = "Lead generated from AI sales agent",
        sr_lead_disease: Optional[str] = "",
        sr_lead_country: Optional[str] = "India",
    ) -> str:
        """Create a new sales lead in the CRM when a customer expresses interest in treatment.
        
        Args:
            first_name: Customer's first name
            middle_name: Customer's middle name or last name
            mobile_no: Customer's mobile number (e.g. '+91 9876543210')
            phone: Customer's secondary phone number
            gender: Gender of the customer (Male, Female, Other)
            sr_lead_message: Summary of interest (e.g., 'Interested in liver treatment')
            sr_lead_notes: Additional notes or call details
            sr_lead_disease: Disease name (e.g. 'Fatty Liver', 'Chronic Kidney Disease')
            sr_lead_country: Country of the customer (defaults to 'India')
        """
        import asyncio
        
        # Normalize and clean phone numbers (e.g., handle double/triple and whitespace)
        clean_mobile = clean_phone_number(mobile_no)
        clean_phone = clean_phone_number(phone) or clean_mobile
        
        lead_data = {
            "first_name": first_name,
            "middle_name": middle_name,
            "sr_lead_country": sr_lead_country,
            "mobile_no": clean_mobile,
            "phone": clean_phone,
            "gender": gender,
            "sr_lead_message": sr_lead_message,
            "sr_lead_notes": sr_lead_notes,
            "sr_lead_disease": sr_lead_disease
        }
        
        # Define the background worker function to avoid conversation latency
        async def submit_mcp_lead(data: dict):
            import aiohttp
            import json
            
            url = os.getenv("MCP_SERVER_URL")
            if not url:
                logger.error("MCP_SERVER_URL is not configured; lead cannot be submitted.")
                return
            if not url.startswith(("http://", "https://")):
                url = "http://" + url
                
            token = os.getenv("MCP_BEARER_TOKEN")
            if not token:
                logger.error("MCP_BEARER_TOKEN is not configured; lead cannot be submitted.")
                return
            
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": self.lead_tool_name,
                    "arguments": data
                }
            }
            
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(f"Background task: Submitting lead {data['first_name']} (Attempt {attempt}/{max_retries})")
                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, headers=headers, json=payload, timeout=10) as response:
                            if response.status == 200:
                                resp_json = await response.json()
                                logger.info(f"Background lead submission succeeded: {resp_json}")
                                return
                            else:
                                logger.error(f"MCP server status {response.status} on attempt {attempt}: {await response.text()}")
                except Exception as e:
                    logger.exception(f"Connection failure on attempt {attempt} for lead {data['first_name']}: {e}")
                
                if attempt < max_retries:
                    await asyncio.sleep(attempt * 2) # Exponential backoff retry
            
            # If all retries failed, log critically and write to local fallback file
            logger.critical(f"❌ Failed to submit lead for {data['first_name']} ({data['mobile_no']}) after {max_retries} attempts. Saving locally.")
            try:
                fallback_path = os.getenv("FAILED_LEADS_PATH", "/tmp/failed_leads.jsonl")
                with open(fallback_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(data) + "\n")
                logger.info(f"Successfully saved lead data locally in offline queue: {fallback_path}")
            except Exception as fe:
                logger.critical(f"❌ Critical failure: Could not save offline fallback queue file: {fe}")
        
        # Schedule it in the event loop immediately (0ms blocking time)
        task = asyncio.create_task(submit_mcp_lead(lead_data))
        self.pending_tasks.append(task)
        
        return "Lead registration initiated successfully in the background. Inform the user registration is complete."


def prewarm(proc: JobProcess):
    """Prewarm function to load Silero VAD for low latency Voice Activity Detection."""
    from livekit.plugins import silero
    
    # Configure optimized VAD options for ultra-low latency & noise filtering
    min_speech_duration = float(os.getenv("VAD_MIN_SPEECH_DURATION", "0.1"))
    min_silence_duration = float(os.getenv("VAD_MIN_SILENCE_DURATION", "0.3"))
    activation_threshold = float(os.getenv("VAD_ACTIVATION_THRESHOLD", "0.65"))
    
    logger.info(
        f"Loading Silero VAD (activation_threshold={activation_threshold}, "
        f"min_speech_duration={min_speech_duration}s, min_silence_duration={min_silence_duration}s)..."
    )
    
    if hasattr(silero.VAD, "load"):
        proc.userdata["vad"] = silero.VAD.load(
            min_speech_duration=min_speech_duration,
            min_silence_duration=min_silence_duration,
            activation_threshold=activation_threshold,
        )
    else:
        proc.userdata["vad"] = silero.VAD()
        
    logger.info("✅ Silero VAD loaded successfully.")


async def entrypoint(ctx: JobContext):
    """Main entrypoint for the Gemini Live voice agent."""
    
    # Logging setup - add context fields for all log entries
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    # Setup Google Cloud project credentials if present locally
    if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS") and os.path.exists("creds.json"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("creds.json")
        logger.info("Using local creds.json for Google Cloud credentials")

    if not os.getenv("GOOGLE_CLOUD_PROJECT") and os.path.exists("creds.json"):
        import json
        try:
            with open("creds.json", "r") as f:
                creds_data = json.load(f)
                os.environ["GOOGLE_CLOUD_PROJECT"] = creds_data.get("project_id", "")
                logger.info(f"Using Google Cloud project: {os.getenv('GOOGLE_CLOUD_PROJECT')}")
        except Exception as e:
            logger.warning(f"Failed to read project_id from creds.json: {e}")

    # Enforce platform choice (always prefer Vertex AI)
    use_vertex = True
    logger.info("Using platform choice: Vertex AI (enforced by agent configuration)")

    # Wait briefly for participant metadata to sync (up to 2 seconds)
    import asyncio
    for _ in range(20):
        if ctx.room.remote_participants:
            break
        await asyncio.sleep(0.1)

    # Extract caller ID and dialed DID from SIP participant attributes if available.
    caller_phone = None
    did_number = None
    trunk_id = None
    for p in ctx.room.remote_participants.values():
        attrs = getattr(p, "attributes", {}) or {}
        caller_phone = attrs.get("sip.phoneNumber") or caller_phone
        did_number = attrs.get("sip.trunkPhoneNumber") or attrs.get("vobiz.did_number") or did_number
        trunk_id = attrs.get("sip.trunkID") or trunk_id
        identity = p.identity
        if caller_phone:
            break
        if identity.startswith("sip_"):
            caller_phone = identity.replace("sip_", "")
            if caller_phone.startswith("00"):
                caller_phone = "+" + caller_phone[2:]
            break
        elif identity.startswith("+") or identity.isdigit():
            caller_phone = identity
            break

    # Fallback: Extract phone number from the room name pattern
    if not caller_phone and ctx.room.name:
        import re
        match = re.search(r'(?:livekit_demo|gemini_live|vobiz)_(\d+)_', ctx.room.name)
        if match:
            raw_phone = match.group(1)
            if raw_phone.startswith("00"):
                caller_phone = "+" + raw_phone[2:]
            else:
                caller_phone = "+" + raw_phone

    metadata = _load_job_metadata(ctx)
    metadata.update(_load_participant_metadata(ctx))
    did_number = did_number or metadata.get("did_number") or metadata.get("to_number")
    if trunk_id and not metadata.get("trunk_id"):
        metadata["trunk_id"] = trunk_id
    config = await fetch_frappe_voice_config(caller_phone=caller_phone, did_number=did_number, metadata=metadata)
    ctx.log_context_fields.update(
        {
            "company_key": metadata.get("company_key") or "",
            "profile_key": metadata.get("profile_key") or config.get("profile_key") or "",
        }
    )
    if config.get("enabled") is False:
        logger.error("Frappe voice profile was not loaded; using configuration failure prompt only: %s", config.get("config_error"))
        config = {
            "agent_name": "Voice Configuration Error",
            "system_prompt": CONFIG_LOAD_FAILURE_PROMPT,
            "greeting_instruction": CONFIG_LOAD_FAILURE_GREETING,
        }
    gemini_config = config.get("gemini") or {}
    mcp_config = config.get("mcp") or {}

    for env_name, value in (
        ("MCP_SERVER_URL", mcp_config.get("server_url")),
        ("MCP_BEARER_TOKEN", mcp_config.get("bearer_token")),
        ("GOOGLE_CLOUD_PROJECT", gemini_config.get("google_cloud_project")),
    ):
        if value:
            os.environ[env_name] = value

    # Configure Gemini Live model name
    model_name = gemini_config.get("model") or os.getenv("GEMINI_LIVE_MODEL")
    if not model_name:
        if use_vertex:
            model_name = "gemini-live-2.5-flash-native-audio"
        else:
            model_name = "gemini-2.5-flash-native-audio-preview-12-2025"
    voice_name = gemini_config.get("voice") or os.getenv("GEMINI_LIVE_VOICE") or "Puck"

    if use_vertex:
        vertex_location = gemini_config.get("vertex_location") or os.getenv("VERTEX_LOCATION", "us-central1")
        logger.info(f"Configuring Gemini Live RealtimeModel using Vertex AI ({vertex_location}) with model {model_name}")
        model = realtime.RealtimeModel(
            model=model_name,
            voice=voice_name,
            vertexai=True,
            project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            location=vertex_location,
            temperature=0.8,
        )
    else:
        logger.info(f"Configuring Gemini Live RealtimeModel using Gemini Developer API (AI Studio) with model {model_name}")
        model = realtime.RealtimeModel(
            model=model_name,
            voice=voice_name,
            api_key=os.getenv("GOOGLE_API_KEY"),
            temperature=0.8,
        )

    # Set up voice AI pipeline with Gemini Live Realtime model
    session = AgentSession(
        llm=model,
        vad=ctx.proc.userdata.get("vad"),
        turn_handling=TurnHandlingOptions(
            endpointing={"mode": "dynamic", "min_delay": 0.3},
        ),
    )

    # Metrics collection for monitoring pipeline performance
    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent):
        """Log metrics when collected."""
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    # Initialize telephony noise cancellation if available
    nc = None
    try:
        from livekit.plugins import noise_cancellation
        nc = noise_cancellation.BVCTelephony()
        logger.info("Enabling BVCTelephony noise cancellation for the room session.")
    except Exception as e:
        logger.warning(f"Could not load noise cancellation plugin: {e}")

    # Start the session with the Frappe-selected voice profile.
    assistant = Assistant(
        caller_phone=caller_phone,
        instructions_override=config.get("system_prompt"),
        agent_name=config.get("agent_name") or DEFAULT_AGENT_NAME,
        lead_tool_name=mcp_config.get("lead_creation_tool_name") or "mcp_create_lead",
        metadata=metadata,
        did_number=did_number,
        allowed_actions=(config.get("guardrails") or {}).get("allowed_actions") or [],
    )

    async def log_usage():
        """Log usage summary and await any background tasks on shutdown."""
        summary = usage_collector.get_summary()
        logger.info(f"Usage: {summary}")
        if hasattr(assistant, "pending_tasks") and assistant.pending_tasks:
            logger.info(f"Awaiting {len(assistant.pending_tasks)} pending CRM submission tasks on shutdown...")
            await asyncio.gather(*assistant.pending_tasks, return_exceptions=True)

    ctx.add_shutdown_callback(log_usage)

    await session.start(
        agent=assistant,
        room=ctx.room,
        room_input_options=RoomInputOptions(
            video_enabled=True,
            noise_cancellation=nc
        ),
    )

    # Connect to the room
    await ctx.connect()

    # Trigger the agent to speak first!
    await session.generate_reply(
        instructions=config.get("greeting_instruction") or DEFAULT_GREETING_INSTRUCTION
    )


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=os.getenv("LIVEKIT_AGENT_NAME", "vobiz-gemini-live"),
        )
    )
