import uuid
import json
import logging
from telegram import Update
from telegram.ext import ContextTypes
from db.database import add_task, add_cron_schedule, get_registered_skills
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, ORCHESTRATOR_MODEL
import re

logger = logging.getLogger(__name__)

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command handler for /start."""
    welcome_text = (
        "🤖 **Welcome to Hermes-ADK 2.0!**\n\n"
        "Here are the available commands:\n"
        "🔹 `/analyze <query>` - Run a developer sandbox code compilation task\n"
        "🔹 `/finance <url>` - Run an automated CDP financial analysis task\n"
        "🔹 `/cron <name> <expr> <payload_json>` - Schedule a recurring task\n"
        "   *Example:* `/cron test '*/5 * * * *' {\"url\":\"https://example.com\"}`\n"
        "🔹 `/skills` - List all verified Dynamic Skills registered in database"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def handle_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command handler for /analyze."""
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("❌ Usage: `/analyze <query_details>`", parse_mode="Markdown")
        return
        
    task_id = f"task_dev_{uuid.uuid4().hex[:8]}"
    chat_id = update.effective_chat.id
    
    # Send initial queueing message
    msg = await update.message.reply_text(
        f"⏳ **Task Accepted & Queued**\n"
        f"🏷️ **ID**: `{task_id}`\n"
        f"📌 **Status**: `pending` (Positioning...)",
        parse_mode="Markdown"
    )
    
    # Store task in SQLite database
    payload = {"query": query}
    add_task(
        task_id=task_id,
        source="telegram",
        agent_type="developer",
        payload=payload,
        tg_chat_id=chat_id,
        tg_msg_id=msg.message_id
    )
    logger.info(f"Queued developer task {task_id} from chat {chat_id}")

async def handle_finance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command handler for /finance."""
    if not context.args:
        await update.message.reply_text("❌ Usage: `/finance <target_url>`", parse_mode="Markdown")
        return
        
    url = context.args[0]
    task_id = f"task_fin_{uuid.uuid4().hex[:8]}"
    chat_id = update.effective_chat.id
    
    # Send initial queueing message
    msg = await update.message.reply_text(
        f"⏳ **CDP Analysis Task Queued**\n"
        f"🏷️ **ID**: `{task_id}`\n"
        f"📌 **Status**: `pending` (Waiting for browser lock...)",
        parse_mode="Markdown"
    )
    
    payload = {"url": url}
    add_task(
        task_id=task_id,
        source="telegram",
        agent_type="finance",
        payload=payload,
        tg_chat_id=chat_id,
        tg_msg_id=msg.message_id
    )
    logger.info(f"Queued finance task {task_id} from chat {chat_id}")

async def handle_cron(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command handler for /cron."""
    args = context.args
    # Expected: /cron name cron_expr payload_json
    if len(args) < 3:
        await update.message.reply_text(
            "❌ Usage: `/cron <name> <expr> <payload_json>`\n"
            "Example: `/cron fetch_news '0 9 * * *' {\"url\":\"https://news.com\"}`",
            parse_mode="Markdown"
        )
        return
        
    name = args[0]
    cron_expr = args[1]
    payload_raw = " ".join(args[2:])
    
    try:
        payload = json.loads(payload_raw)
    except Exception as e:
        await update.message.reply_text(f"❌ Invalid JSON payload: {e}")
        return
        
    schedule_id = f"cron_{uuid.uuid4().hex[:8]}"
    
    # Register the schedule
    success = add_cron_schedule(
        schedule_id=schedule_id,
        name=name,
        cron_expr=cron_expr,
        task_payload=payload
    )
    
    if success:
        await update.message.reply_text(
            f"✅ **Cron Schedule Registered**\n"
            f"🏷️ **ID**: `{schedule_id}`\n"
            f"📝 **Name**: `{name}`\n"
            f"🕒 **Cron**: `{cron_expr}`",
            parse_mode="Markdown"
        )
        logger.info(f"Registered cron schedule {schedule_id}")
    else:
        await update.message.reply_text("❌ Failed to register schedule in database.")

async def handle_list_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command handler for /skills to list registered dynamic skills."""
    skills = get_registered_skills()
    if not skills:
        await update.message.reply_text("🗂️ No dynamic skills registered in database.")
        return
        
    text = "🗂️ **Registered Dynamic Skills**:\n\n"
    for s in skills:
        # Get path relative to Hmsdk
        rel_path = s['path'].split("Hmsdk/")[-1] if "Hmsdk/" in s['path'] else s['path']
        text += f"🔹 **Name**: `{s['name']}`\n"
        text += f"   *Path*: `{rel_path}`\n"
        text += f"   *Env ID*: `{s['sandbox_env_id']}`\n"
        text += f"   *Compiled*: {s['created_at']}\n\n"
        
    await update.message.reply_text(text, parse_mode="Markdown")

async def handle_unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Orchestrator (Main Agent) fallback:
    1. Parse user's natural language input.
    2. Use Gemini-3.5-flash to classify intent: direct response vs routing to subagents.
    3. Either reply directly or push a task to the queue and notify the user.
    """
    user_text = update.message.text
    chat_id = update.effective_chat.id
    
    # Send a typing status indicator
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    
    client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()
    
    prompt = (
        f"You are the Orchestrator (Main Agent) of the Hermes-ADK 2.0 AI system. "
        f"Analyze the user's message and determine if it's general conversation/question, or if it requests "
        f"a specific action that should be delegated to one of our specialized subagents:\n"
        f"- Developer Agent: Handles coding requests, writing python modules, compiling skills, or sandboxed tests.\n"
        f"- Finance Agent: Handles fetching and analyzing financial/stock/business news from a specific web URL.\n"
        f"- Scheduler Agent: Handles setting up cron schedules or recurring tasks from natural language descriptions.\n\n"
        f"User message: \"{user_text}\"\n\n"
        f"Classify the intent and reply in a strict JSON format matching this schema:\n"
        f"{{\n"
        f"  \"action\": \"direct_response\" | \"route_developer\" | \"route_finance\" | \"route_scheduler\",\n"
        f"  \"response\": \"Markdown text response if action is direct_response, else empty.\",\n"
        f"  \"payload\": {{\n"
        f"     # if route_developer: {{\"query\": \"user query\"}}\n"
        f"     # if route_finance: {{\"url\": \"http://extracted-url-to-scrape\"}}\n"
        f"     # if route_scheduler: {{\"query\": \"schedule description\"}}\n"
        f"  }}\n"
        f"}}\n"
        f"Ensure all double quotes are properly escaped to make the response a valid JSON."
    )
    
    try:
        response = client.models.generate_content(
            model=ORCHESTRATOR_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        
        data = json.loads(response.text)
        action = data.get("action", "direct_response")
        direct_reply = data.get("response", "")
        payload = data.get("payload", {})
    except Exception as e:
        logger.error(f"Orchestrator failed to classify intent: {e}. Raw response: {response.text if 'response' in locals() else ''}")
        await update.message.reply_text("🤖 Sorry, I had trouble understanding that. Please use a direct command or try again.")
        return
        
    if action == "direct_response":
        if not direct_reply:
            direct_reply = "🤖 I'm here. How can I help you?"
        await update.message.reply_text(direct_reply, parse_mode="Markdown")
        
    elif action == "route_developer":
        dev_query = payload.get("query") or user_text
        task_id = f"task_dev_{uuid.uuid4().hex[:8]}"
        msg = await update.message.reply_text(
            f"🧠 **Orchestrator**: Routing to **Developer Agent**...\n"
            f"⏳ **Task Queued**\n"
            f"🏷️ **ID**: `{task_id}`\n"
            f"📝 **Query**: \"{dev_query}\"",
            parse_mode="Markdown"
        )
        add_task(
            task_id=task_id,
            source="telegram",
            agent_type="developer",
            payload={"query": dev_query},
            tg_chat_id=chat_id,
            tg_msg_id=msg.message_id
        )
        
    elif action == "route_finance":
        url = payload.get("url")
        if not url:
            urls = re.findall(r'https?://[^\s]+', user_text)
            if urls:
                url = urls[0]
                
        if not url:
            await update.message.reply_text("❌ Orchestrator detected a finance request, but could not extract a valid URL to analyze.")
            return
            
        task_id = f"task_fin_{uuid.uuid4().hex[:8]}"
        msg = await update.message.reply_text(
            f"🧠 **Orchestrator**: Routing to **Finance Agent**...\n"
            f"⏳ **CDP Analysis Task Queued**\n"
            f"🏷️ **ID**: `{task_id}`\n"
            f"🔗 **Scraping**: {url}",
            parse_mode="Markdown"
        )
        add_task(
            task_id=task_id,
            source="telegram",
            agent_type="finance",
            payload={"url": url},
            tg_chat_id=chat_id,
            tg_msg_id=msg.message_id
        )
        
    elif action == "route_scheduler":
        sch_query = payload.get("query") or user_text
        task_id = f"task_sch_{uuid.uuid4().hex[:8]}"
        msg = await update.message.reply_text(
            f"🧠 **Orchestrator**: Routing to **Scheduler Agent**...\n"
            f"⏳ **Parsing Schedule Task Queued**\n"
            f"🏷️ **ID**: `{task_id}`\n"
            f"🕒 **Schedule**: \"{sch_query}\"",
            parse_mode="Markdown"
        )
        add_task(
            task_id=task_id,
            source="telegram",
            agent_type="scheduler",
            payload={"query": sch_query},
            tg_chat_id=chat_id,
            tg_msg_id=msg.message_id
        )
