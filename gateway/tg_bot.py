import asyncio
import logging
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.error import TelegramError
import config
from db.database import get_db_conn
from gateway.handlers import handle_start, handle_analyze, handle_finance, handle_cron, handle_list_skills, handle_unknown_text

logger = logging.getLogger(__name__)

from db.database import get_db_conn, update_task_status

async def notification_loop(bot):
    """
    Background worker loop that checks the SQLite task queue for status updates
    and updates the corresponding Telegram messages asynchronously.
    Uses database-persisted tg_notified_status to ensure reliability and idempotency.
    """
    logger.info("Telegram notification loop started.")
    await asyncio.sleep(5)  # Let bot initialize
    
    while True:
        try:
            conn = get_db_conn()
            cursor = conn.cursor()
            
            # Fetch tasks that need Telegram updates and haven't completed final notification
            tasks = cursor.execute(
                """
                SELECT task_id, status, tg_chat_id, tg_msg_id, result_data, error_log, tg_notified_status, artifact_path
                FROM task_queue
                WHERE tg_chat_id IS NOT NULL AND tg_msg_id IS NOT NULL AND tg_notified_status != 'notified'
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
                notified_status = task['tg_notified_status'] or "pending"
                artifact_path = task['artifact_path']
                
                # Case 1: Task transitioned to running
                if status == 'running' and notified_status == 'pending':
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=f"🏃 **Task is running...**\n🏷️ **ID**: `{task_id}`",
                            parse_mode="Markdown"
                        )
                        update_task_status(task_id, status, tg_notified_status='running')
                        logger.info(f"Updated TG chat {chat_id} msg {msg_id}: task {task_id} running.")
                        # Throttling to prevent API rate limiting
                        await asyncio.sleep(1.0)
                    except TelegramError as te:
                        logger.warning(f"Failed to edit message for running task {task_id}: {te}")
                        te_str = str(te).lower()
                        if "not modified" in te_str or "not found" in te_str or "chat not found" in te_str:
                            update_task_status(task_id, status, tg_notified_status='running')
                        elif "retry in" in te_str:
                            # Hit rate limits, back off
                            await asyncio.sleep(5.0)
                        
                # Case 2: Task finished (completed or failed)
                elif status in ('completed', 'failed') and notified_status in ('pending', 'running'):
                    try:
                        import html
                        import os
                        if status == 'completed':
                            # Truncate result data if too long for a single telegram message (limit is 4096)
                            safe_result = html.escape(result_data[:3000])
                            if len(result_data) > 3000:
                                safe_result += "\n\n<b>(Truncated due to length)</b>"
                                
                            text = (
                                f"✅ <b>Task Completed!</b>\n"
                                f"🏷️ <b>ID</b>: <code>{task_id}</code>\n\n"
                                f"📊 <b>Result</b>:\n{safe_result}"
                            )
                        else:
                            safe_error = html.escape(error_log)
                            text = (
                                f"❌ <b>Task Failed!</b>\n"
                                f"🏷️ <b>ID</b>: <code>{task_id}</code>\n\n"
                                f"⚠️ <b>Error Log</b>:\n<pre>{safe_error}</pre>"
                            )
                            
                        if msg_id == -1:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=text,
                                parse_mode="HTML"
                            )
                        else:
                            await bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=msg_id,
                                text=text,
                                parse_mode="HTML"
                            )
                            
                        # If artifact_path is set and file exists, upload and send it to the user!
                        if status == 'completed' and artifact_path and os.path.exists(artifact_path):
                            logger.info(f"Uploading artifact {artifact_path} to TG chat {chat_id}...")
                            ext = os.path.splitext(artifact_path.lower())[1]
                            
                            # Determine type and upload
                            try:
                                if ext in ('.mp4', '.avi', '.mov', '.mkv', '.webm'):
                                    with open(artifact_path, 'rb') as video_file:
                                        await bot.send_video(
                                            chat_id=chat_id,
                                            video=video_file,
                                            caption=f"🎥 Video Attachment for Task <code>{task_id}</code>",
                                            parse_mode="HTML"
                                        )
                                elif ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
                                    with open(artifact_path, 'rb') as photo_file:
                                        await bot.send_photo(
                                            chat_id=chat_id,
                                            photo=photo_file,
                                            caption=f"🖼️ Image Attachment for Task <code>{task_id}</code>",
                                            parse_mode="HTML"
                                        )
                                else:
                                    with open(artifact_path, 'rb') as doc_file:
                                        await bot.send_document(
                                            chat_id=chat_id,
                                            document=doc_file,
                                            filename=os.path.basename(artifact_path),
                                            caption=f"📄 File Attachment for Task <code>{task_id}</code>",
                                            parse_mode="HTML"
                                        )
                                logger.info(f"Successfully sent artifact {artifact_path} to chat {chat_id}.")
                            except Exception as upload_err:
                                logger.error(f"Failed to upload task {task_id} artifact {artifact_path}: {upload_err}")
                                # Send text notification of failure to upload
                                await bot.send_message(
                                    chat_id=chat_id,
                                    text=f"⚠️ <b>Task Artifact Upload Failed</b>\nID: <code>{task_id}</code>\nError: <code>{html.escape(str(upload_err))}</code>",
                                    parse_mode="HTML"
                                )
                                
                        update_task_status(task_id, status, tg_notified_status='notified')
                        logger.info(f"Updated TG chat {chat_id} msg {msg_id}: task {task_id} {status}.")
                        # Throttling to prevent API rate limiting
                        await asyncio.sleep(1.0)
                    except TelegramError as te:
                        logger.warning(f"Failed to edit message for finished task {task_id}: {te}")
                        te_str = str(te).lower()
                        if "not modified" in te_str or "not found" in te_str or "chat not found" in te_str:
                            update_task_status(task_id, status, tg_notified_status='notified')
                        elif "retry in" in te_str:
                            # Hit rate limits, back off
                            await asyncio.sleep(5.0)
                        
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
