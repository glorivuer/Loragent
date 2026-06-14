import uuid
import json
import logging
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, ORCHESTRATOR_MODEL
from db.database import add_cron_schedule

logger = logging.getLogger(__name__)

async def run(payload: dict) -> str:
    """
    Scheduler Agent:
    Translates natural language schedule descriptions into standardized cron expressions and registers them.
    """
    query = payload.get("query", "")
    if not query:
        # Check if direct registration properties are provided
        name = payload.get("name")
        cron_expr = payload.get("cron_expr")
        task_payload = payload.get("task_payload")
        
        if name and cron_expr and task_payload:
            schedule_id = f"cron_{uuid.uuid4().hex[:8]}"
            success = add_cron_schedule(schedule_id, name, cron_expr, task_payload)
            if success:
                return f"✅ Registered schedule '{name}' successfully."
            raise RuntimeError("Database registration failed.")
            
        raise ValueError("No cron query or direct parameters specified.")
        
    logger.info(f"Scheduler Subagent: Translating NL cron request: {query}")
    
    # Use Gemini to parse the natural language query into cron details
    client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()
    
    prompt = (
        f"You are an AI assistant. Translate the following natural language schedule request into a standard crontab expression: '{query}'.\n"
        f"Generate a valid JSON object matching the following structure:\n"
        f"{{\n"
        f"  \"cron_expr\": \"standard crontab expression matching the request (e.g., '0 9 * * 1-5')\",\n"
        f"  \"name\": \"short_descriptive_name_in_snake_case\",\n"
        f"  \"task_payload\": {{\n"
        f"     \"url\": \"https://example.com/mock-endpoint-or-url\",\n"
        f"     \"agent_type\": \"finance\"\n"
        f"  }}\n"
        f"}}\n"
        f"Ensure all values are valid JSON."
    )
    
    response = client.models.generate_content(
        model=ORCHESTRATOR_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    
    try:
        data = json.loads(response.text)
        cron_expr = data.get("cron_expr").strip()
        name = data.get("name").strip().lower()
        task_payload = data.get("task_payload", {})
    except Exception as e:
        logger.error(f"Failed to parse Gemini output into JSON: {e}. Raw: {response.text}")
        raise RuntimeError(f"Cron parsing failed: {e}")
        
    schedule_id = f"cron_{uuid.uuid4().hex[:8]}"
    
    # Register schedule
    success = add_cron_schedule(
        schedule_id=schedule_id,
        name=name,
        cron_expr=cron_expr,
        task_payload=task_payload
    )
    
    if success:
        return (
            f"✅ **NL Cron Schedule Registered!**\n"
            f"🏷️ **ID**: `{schedule_id}`\n"
            f"📝 **Name**: `{name}`\n"
            f"🕒 **Cron Expression**: `{cron_expr}`\n"
            f"📦 **Payload**: `{json.dumps(task_payload)}`"
        )
    else:
        raise RuntimeError("Failed to insert schedule into SQLite.")
