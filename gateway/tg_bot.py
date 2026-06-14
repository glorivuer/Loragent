import asyncio
import logging
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.error import TelegramError
import config
from db.database import get_db_conn
from gateway.handlers import handle_start, handle_analyze, handle_finance, handle_cron, handle_list_skills, handle_unknown_text

logger = logging.getLogger(__name__)

# In-memory sets to track notification states and avoid repeating edits
notified_running = set()
notified_finished = set()

async def notification_loop(bot):
    """
    Background worker loop that checks the SQLite task queue for status updates
    and updates the corresponding Telegram messages asynchronously.
    """
    logger.info("Telegram notification loop started.")
    await asyncio.sleep(5)  # Let bot initialize
    
    while True:
        try:
            conn = get_db_conn()
            cursor = conn.cursor()
            
            # Fetch tasks that need updates (we only care about telegram sourced tasks)
            tasks = cursor.execute(
                """
                SELECT task_id, status, tg_chat_id, tg_msg_id, result_data, error_log, updated_at
                FROM task_queue
                WHERE tg_chat_id IS NOT NULL AND tg_msg_id IS NOT NULL
                """
            ).fetchall()
            conn.close()
            
            for task in tasks:
                task_id = task['task_id']
                status = task['status']
                chat_id = task['tg_chat_id']
                msg_id = task['tg_msg_id']
                result_data = task['result_data'] or ""
                error_log = task['error_log'] or ""
                
                # Case 1: Task transitioned to running
                if status == 'running' and task_id not in notified_running:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=f"🏃 **Task is running...**\n🏷️ **ID**: `{task_id}`",
                            parse_mode="Markdown"
                        )
                        notified_running.add(task_id)
                        logger.info(f"Updated TG chat {chat_id} msg {msg_id}: task {task_id} running.")
                    except TelegramError as te:
                        logger.warning(f"Failed to edit message for running task {task_id}: {te}")
                        
                # Case 2: Task finished (completed or failed)
                elif status in ('completed', 'failed') and task_id not in notified_finished:
                    try:
                        if status == 'completed':
                            # Truncate result data if too long for a single telegram message (limit is 4096)
                            formatted_result = result_data[:3000]
                            if len(result_data) > 3000:
                                formatted_result += "\n\n*(Truncated due to length)*"
                                
                            text = (
                                f"✅ **Task Completed!**\n"
                                f"🏷️ **ID**: `{task_id}`\n\n"
                                f"📊 **Result**:\n{formatted_result}"
                            )
                        else:
                            text = (
                                f"❌ **Task Failed!**\n"
                                f"🏷️ **ID**: `{task_id}`\n\n"
                                f"⚠️ **Error Log**:\n`{error_log}`"
                            )
                            
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=text,
                            parse_mode="Markdown"
                        )
                        notified_finished.add(task_id)
                        logger.info(f"Updated TG chat {chat_id} msg {msg_id}: task {task_id} {status}.")
                    except TelegramError as te:
                        logger.warning(f"Failed to edit message for finished task {task_id}: {te}")
                        
        except Exception as e:
            logger.error(f"Error in TG notification loop: {e}")
            
        await asyncio.sleep(5)

async def start_tg_bot():
    """Starts the Telegram bot and runs the application."""
    if not config.TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN is missing in environment variables. Bot will not run.")
        return
        
    application = Application.builder().token(config.TELEGRAM_TOKEN).build()
    
    # Register command handlers
    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(CommandHandler("analyze", handle_analyze))
    application.add_handler(CommandHandler("finance", handle_finance))
    application.add_handler(CommandHandler("cron", handle_cron))
    application.add_handler(CommandHandler("skills", handle_list_skills))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown_text))
    
    # Start notification loop background task
    asyncio.create_task(notification_loop(application.bot))
    
    logger.info("Initializing Telegram bot...")
    await application.initialize()
    await application.start()
    logger.info("Telegram bot started successfully. Entering polling loop...")
    await application.updater.start_polling()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    
    # Run bot independently if executed directly
    loop = asyncio.get_event_loop()
    loop.create_task(start_tg_bot())
    loop.run_forever()
