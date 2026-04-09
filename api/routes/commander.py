"""
KJ Commander — AI Agent Endpoint
KJLE - King James Lead Empire
Route: POST /kjle/v1/commander/chat

Receives user messages, calls Claude API, executes KJLE API
actions, and returns intelligent responses. No Lovable Cloud needed.
"""

import logging
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from ..database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
KJLE_BASE = "https://kjle-api.onrender.com/kjle/v1"
KJLE_KEY = "kjle-prod-2026-secret"

SYSTEM_PROMPT = """You are KJ Commander, the AI agent for KJLE (King James Lead Empire).
You have full access to the KJLE API at https://kjle-api.onrender.com/kjle/v1
using header x-api-key: kjle-prod-2026-secret.

You are running inside a FastAPI backend — you CAN make real HTTP calls to the KJLE API.
When the user asks you to do something, DO IT. Execute it and report back with real data.

ACTIONS YOU CAN TAKE (use httpx to call these):
- Find leads: GET /leads?niche_slug=X&state=X&min_pain=X&max_pain=X&segment_label=X&page_size=500
- Get stats: GET /leads/stats
- Get pipeline: GET /pipeline/status
- Get scheduler status: GET /scheduler/status
- Run classifier: POST /scheduler/run/classify_segments
- Run Stage 1: POST /scheduler/run/enrich_stage1
- Run Stage 3: POST /scheduler/run/enrich_stage3_nightly
- Run Stage 4: POST /scheduler/run/enrich_stage4_nightly
- Get budget: GET /budget/summary
- Get niche breakdown: GET /segments/by-niche
- Get pain by niche: GET /pain/by-niche

RESPONSE STYLE:
- Military command style — direct and decisive
- Always confirm what action you took with real results
- Show key numbers prominently
- If you find leads, show top 5 in a clean list
- Use 👑 for important results
- Keep responses concise — this is a command center not a chatbot

KNOWLEDGE BASE:
- HOT leads = pain score >= 70
- WARM leads = pain score 40-69
- COLD leads = pain score < 40
- Stage 1 = free website scrape (runs every 12hrs, 200 leads)
- Stage 3 = Outscraper Google Maps ($0.002/lead, pain >= 60)
- Stage 4 = Firecrawl deep crawl ($0.005/lead, pain >= 75)
- No Chatbot filter requires Stage 4 enrichment
- Budget Guard protects spend automatically
- 8 CSVs upload nightly at 10pm
- Email cleaning runs nightly at midnight via Truelist
- Classifier runs every 6 hours automatically
- Current API key: kjle-prod-2026-secret

When the user asks for leads, ALWAYS call the API and return real data.
When the user asks for status, ALWAYS call the API and return real numbers.
Never make up data — always fetch it live."""


class ChatRequest(BaseModel):
    message: str
    conversation_history: Optional[list] = []
    anthropic_api_key: str


class ChatResponse(BaseModel):
    response: str
    actions_taken: list = []


async def call_kjle_api(method: str, path: str, params: dict = None) -> dict:
    """Helper to call KJLE API internally."""
    url = f"{KJLE_BASE}{path}"
    headers = {"x-api-key": KJLE_KEY}
    async with httpx.AsyncClient(timeout=30.0) as client:
        if method.upper() == "GET":
            r = await client.get(url, headers=headers, params=params or {})
        else:
            r = await client.post(url, headers=headers)
        return r.json()


@router.post("/commander/chat")
async def commander_chat(body: ChatRequest):
    """
    KJ Commander AI agent endpoint.
    Receives user message, calls Claude, executes KJLE API actions,
    returns intelligent response with real data.
    """
    if not body.anthropic_api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key required")

    # Build messages for Claude
    messages = list(body.conversation_history or [])
    messages.append({"role": "user", "content": body.message})

    # Detect intent and pre-fetch relevant data to include as context
    msg_lower = body.message.lower()
    context_data = {}
    actions_taken = []

    try:
        # Pre-fetch data based on user intent
        if any(w in msg_lower for w in ["status", "how many", "total", "leads", "stats", "empire"]):
            stats = await call_kjle_api("GET", "/leads/stats")
            context_data["lead_stats"] = stats
            actions_taken.append("Fetched lead statistics")

        if any(w in msg_lower for w in ["scheduler", "jobs", "running", "nightly", "enrichment status"]):
            sched = await call_kjle_api("GET", "/scheduler/status")
            context_data["scheduler"] = sched
            actions_taken.append("Fetched scheduler status")

        if any(w in msg_lower for w in ["budget", "cost", "spend", "spending"]):
            budget = await call_kjle_api("GET", "/budget/summary")
            context_data["budget"] = budget
            actions_taken.append("Fetched budget summary")

        if any(w in msg_lower for w in ["niche", "niches", "breakdown", "by niche"]):
            niches = await call_kjle_api("GET", "/segments/by-niche")
            context_data["niches"] = niches
            actions_taken.append("Fetched niche breakdown")

        # Handle lead searches
        if any(w in msg_lower for w in ["find", "show", "give me", "get", "send", "export", "warm leads", "hot leads", "cold leads"]):
            params = {"page_size": 10, "order_by": "pain_score", "order_dir": "desc"}

            # Extract niche
            niches_list = ["hvac", "plumbing", "roofing", "cleaning", "gym", "dental",
                          "landscaping", "electrical", "painting", "pest_control", "lawyer",
                          "chiropractic", "salon", "restaurant", "auto", "moving"]
            for n in niches_list:
                if n in msg_lower:
                    params["niche_slug"] = n
                    break

            # Extract state
            states = {"texas": "TX", "california": "CA", "florida": "FL", "new york": "NY",
                     "georgia": "GA", "ohio": "OH", "michigan": "MI", "illinois": "IL",
                     "tx": "TX", "ca": "CA", "fl": "FL", "ny": "NY", "ga": "GA"}
            for state_name, state_code in states.items():
                if state_name in msg_lower:
                    params["state"] = state_code
                    break

            # Extract segment
            if "hot" in msg_lower:
                params["min_pain"] = 70
            elif "warm" in msg_lower:
                params["min_pain"] = 40
                params["max_pain"] = 69
            elif "cold" in msg_lower:
                params["max_pain"] = 39

            # Extract count
            import re
            numbers = re.findall(r'\b(\d+)\b', body.message)
            if numbers:
                requested = int(numbers[0])
                params["page_size"] = min(requested, 500)

            leads_result = await call_kjle_api("GET", "/leads", params)
            context_data["leads_result"] = {
                "total": leads_result.get("total", 0),
                "filters_applied": params,
                "top_leads": leads_result.get("leads", [])[:5]
            }
            actions_taken.append(f"Searched leads with filters: {params}")

        # Handle trigger actions
        if any(w in msg_lower for w in ["run classifier", "classify", "reclassify"]):
            result = await call_kjle_api("POST", "/scheduler/run/classify_segments")
            context_data["classifier_result"] = result
            actions_taken.append("Triggered classifier job")

        if any(w in msg_lower for w in ["run stage 3", "run outscraper", "stage 3 now"]):
            result = await call_kjle_api("POST", "/scheduler/run/enrich_stage3_nightly")
            context_data["stage3_result"] = result
            actions_taken.append("Triggered Stage 3 enrichment")

        if any(w in msg_lower for w in ["run stage 4", "run firecrawl", "stage 4 now"]):
            result = await call_kjle_api("POST", "/scheduler/run/enrich_stage4_nightly")
            context_data["stage4_result"] = result
            actions_taken.append("Triggered Stage 4 enrichment")

    except Exception as e:
        logger.warning(f"[Commander] Data fetch error: {e}")

    # Build enhanced message with real data
    enhanced_message = body.message
    if context_data:
        enhanced_message += f"\n\n[LIVE DATA FROM KJLE API]: {context_data}"

    # Update messages with enhanced content
    messages[-1]["content"] = enhanced_message

    # Call Claude API
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": body.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "system": SYSTEM_PROMPT,
                    "messages": messages,
                },
            )
            data = r.json()

        if r.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Claude API error: {data}")

        response_text = data["content"][0]["text"]

        return {
            "response": response_text,
            "actions_taken": actions_taken,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Commander] Claude API error: {e}")
        raise HTTPException(status_code=500, detail=f"Commander error: {str(e)}")
