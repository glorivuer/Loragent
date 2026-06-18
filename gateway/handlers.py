import uuid
import json
import logging
from telegram import Update
from telegram.ext import ContextTypes
from db.database import add_task, add_cron_schedule, get_registered_skills, get_all_skills
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
    """Command handler for /skills to list all static and dynamic skills."""
    import html
    skills = get_all_skills()
    if not skills:
        await update.message.reply_text("🗂️ No dynamic or static skills registered.")
        return
        
    text = "🗂️ <b>Available Skills (Static & Dynamic)</b>:\n\n"
    for s in skills:
        # Get path relative to Hmsdk
        rel_path = s['path'].split("Hmsdk/")[-1] if "Hmsdk/" in s['path'] else s['path']
        name_esc = html.escape(s['name'])
        path_esc = html.escape(rel_path)
        type_str = 'Static (Pre-installed)' if s['sandbox_env_id'] == 'static' else 'Dynamic'
        desc_esc = html.escape(s.get('description', 'No description.'))
        
        text += f"🔹 <b>Name</b>: <code>{name_esc}</code>\n"
        text += f"   <i>Path</i>: <code>{path_esc}</code>\n"
        text += f"   <i>Type</i>: {type_str}\n"
        text += f"   <i>Description</i>: {desc_esc}\n\n"
        
    await update.message.reply_text(text, parse_mode="HTML")

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
    
    # Fetch registered skills dynamically to make Orchestrator aware of them
    skills = get_all_skills()
    skills_list_str = ""
    for s in skills:
        skills_list_str += f"- Skill Name: `{s['name']}` (Type: {'Static' if s['sandbox_env_id'] == 'static' else 'Dynamic'}, Path: `{s['path']}`)\n"
    
    system_instruction = (
        "You are the Orchestrator (Main Agent) of the Hermes-ADK 2.0 AI system.\n"
        "Analyze the user's message and determine if it's general conversation/question, or if it requests "
        "a specific action that should be delegated to one of our specialized subagents:\n"
        "- Developer Agent: Handles coding requests, writing python modules, compiling/creating new skills, running sandboxed/local tests, or executing an existing dynamic skill.\n"
        "- Finance Agent: Handles fetching and analyzing financial/stock/business news from a specific web URL.\n"
        "- Scheduler Agent: Handles setting up cron schedules or recurring tasks from natural language descriptions.\n"
        "- General Agent: Handles general requests, questions, document/file analysis (e.g. reading/analyzing local files like karpathy.md), local workspace inspection, and shell command executions.\n\n"
        f"Available Dynamic Skills registered in the database:\n{skills_list_str or 'None'}\n\n"
        "Instructions:\n"
        "1. If the user wants to execute/run an existing dynamic skill (e.g. 'run skill calculate_fibonacci with arg 10' or 'use skill X to ...'), "
        "you should delegate to the Developer Agent (action: 'route_developer') with the query detailing the execution request.\n"
        "2. If the user asks general questions or asks to analyze, view, or parse local documents or files (like karpathy.md), "
        "delegate to the General Agent (action: 'route_general').\n"
        "3. Classify the intent and reply in a strict JSON format matching this schema:\n"
        "{\n"
        "  \"action\": \"direct_response\" | \"route_developer\" | \"route_finance\" | \"route_scheduler\" | \"route_general\",\n"
        "  \"response\": \"Markdown text response if action is direct_response, else empty.\",\n"
        "  \"payload\": {\n"
        "     # if route_developer: {\"query\": \"user query\"}\n"
        "     # if route_finance: {\"url\": \"http://extracted-url-to-scrape\"}\n"
        "     # if route_scheduler: {\"query\": \"schedule description\"}\n"
        "     # if route_general: {\"query\": \"user query\"}\n"
        "  }\n"
        "}\n"
        "Ensure all double quotes are properly escaped to make the response a valid JSON."
    )
    
    try:
        response = client.models.generate_content(
            model=ORCHESTRATOR_MODEL,
            contents=user_text,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json"
            )
        )
        
        data = json.loads(response.text)
        action = data.get("action", "direct_response")
        direct_reply = data.get("response", "")
        payload = data.get("payload", {})
    except Exception as e:
        logger.error(f"Orchestrator failed to classify intent: {e}. Raw response: {response.text if 'response' in locals() else ''}")
        await update.message.reply_text("🤖 Sorry, I had trouble understanding that. Please use a direct command or try again.")
        return

        
    import html

    if action == "direct_response":
        if not direct_reply:
            direct_reply = "🤖 I'm here. How can I help you?"
        # Fallback to direct text if markdown has parse errors
        try:
            await update.message.reply_text(direct_reply, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(direct_reply)
        
    elif action == "route_developer":
        dev_query = payload.get("query") or user_text
        task_id = f"task_dev_{uuid.uuid4().hex[:8]}"
        safe_query = html.escape(dev_query)
        msg = await update.message.reply_text(
            f"🧠 <b>Orchestrator</b>: Routing to <b>Developer Agent</b>...\n"
            f"⏳ <b>Task Queued</b>\n"
            f"🏷️ <b>ID</b>: <code>{task_id}</code>\n"
            f"📝 <b>Query</b>: \"{safe_query}\"",
            parse_mode="HTML"
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
        safe_url = html.escape(url)
        msg = await update.message.reply_text(
            f"🧠 <b>Orchestrator</b>: Routing to <b>Finance Agent</b>...\n"
            f"⏳ <b>CDP Analysis Task Queued</b>\n"
            f"🏷️ <b>ID</b>: <code>{task_id}</code>\n"
            f"🔗 <b>Scraping</b>: {safe_url}",
            parse_mode="HTML"
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
        safe_sch = html.escape(sch_query)
        msg = await update.message.reply_text(
            f"🧠 <b>Orchestrator</b>: Routing to <b>Scheduler Agent</b>...\n"
            f"⏳ <b>Parsing Schedule Task Queued</b>\n"
            f"🏷️ <b>ID</b>: <code>{task_id}</code>\n"
            f"🕒 <b>Schedule</b>: \"{safe_sch}\"",
            parse_mode="HTML"
        )
        add_task(
            task_id=task_id,
            source="telegram",
            agent_type="scheduler",
            payload={"query": sch_query},
            tg_chat_id=chat_id,
            tg_msg_id=msg.message_id
        )
        
    elif action == "route_general":
        gen_query = payload.get("query") or user_text
        task_id = f"task_gen_{uuid.uuid4().hex[:8]}"
        safe_gen = html.escape(gen_query)
        msg = await update.message.reply_text(
            f"🧠 <b>Orchestrator</b>: Routing to <b>General Agent</b>...\n"
            f"⏳ <b>Task Queued</b>\n"
            f"🏷️ <b>ID</b>: <code>{task_id}</code>\n"
            f"📝 <b>Query</b>: \"{safe_gen}\"",
            parse_mode="HTML"
        )
        add_task(
            task_id=task_id,
            source="telegram",
            agent_type="general",
            payload={"query": gen_query},
            tg_chat_id=chat_id,
            tg_msg_id=msg.message_id
        )
