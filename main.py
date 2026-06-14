import asyncio
import logging
from db.database import init_db
from gateway.tg_bot import start_tg_bot
from scheduler.heartbeat import scheduler_loop

# Configure logging format and level
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s"
)
logger = logging.getLogger("hermes_adk_main")

async def main():
    logger.info("==============================================")
    logger.info("  Starting Project Hermes-ADK 2.0 (Hmsdk)...  ")
    logger.info("==============================================")
    
    # 1. Initialize DB directories and schema
    try:
        init_db()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        return
        
    # 2. Concurrently run Telegram Bot Gateway and Scheduler loop
    logger.info("Launching Telegram Bot Gateway and Scheduler Daemon...")
    try:
        await asyncio.gather(
            start_tg_bot(),
            scheduler_loop()
        )
    except (KeyboardInterrupt, SystemExit):
        logger.info("Graceful shutdown initiated.")
    except Exception as e:
        logger.critical(f"System crash in main unified runner: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Hermes-ADK 2.0 exited.")
