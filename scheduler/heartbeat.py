import asyncio
import redis
import logging
import sqlite3
from datetime import datetime, time
from croniter import croniter
import config
from db.database import get_db_conn, update_task_status

logger = logging.getLogger(__name__)

# Initialize Redis client with in-memory MockRedis fallback
try:
    r = redis.Redis(
        host=config.REDIS_HOST,
        port=config.REDIS_PORT,
        db=config.REDIS_DB,
        decode_responses=True
    )
    r.ping()
    logger.info("Successfully connected to Redis server.")
except Exception as e:
    logger.warning(f"Failed to connect to Redis: {e}. Falling back to in-memory MockRedis.")
    from db.redis_mock import MockRedis
    r = MockRedis()

def is_in_drain_window() -> bool:
    """Checks if the current time falls within the 03:50 - 04:00 browser maintenance window."""
    now = datetime.now().time()
    
    end_hour = config.RESTART_HOUR
    end_minute = config.RESTART_MINUTE + 1
    if end_minute >= 60:
        end_minute = end_minute % 60
        end_hour = (end_hour + 1) % 24
        
    start = time(config.DRAIN_START_HOUR, config.DRAIN_START_MINUTE)
    end = time(end_hour, end_minute)
    return start <= now <= end

async def restart_chromium_process():
    """Restarts host Chromium to clear memory leaks and bloated cache."""
    logger.warning("Initiating physical Chromium restart...")
    try:
        # Kill both Chrome and Chromium debugging processes
        kill_chrome = await asyncio.create_subprocess_shell("pkill -f 'chrome|chromium'")
        await kill_chrome.wait()
        logger.info("Chromium processes terminated.")
        
        # Give OS a moment to release resources
        await asyncio.sleep(2)
        
        # Re-launch Chromium under remote debugging port
        cmd = (
            "chromium-browser --remote-debugging-port=9222 "
            "--user-data-dir=/home/ubuntu/.config/chromium-hermes "
            "--no-first-run --disable-gpu --window-size=1280,720 &"
        )
        # Note: If chromium-browser is not available, try google-chrome
        launch = await asyncio.create_subprocess_shell(cmd)
        await launch.wait()
        
        # Allow browser time to initialize
        await asyncio.sleep(5)
        logger.info("Chromium remote debugger restarted successfully on port 9222.")
    except Exception as e:
        logger.error(f"Failed to restart Chromium debugger: {e}")

async def check_and_update_scheduler_state():
    """Manages scheduler modes: running -> draining -> offline"""
    current_mode = r.get("scheduler:mode") or "running"
    
    if is_in_drain_window():
        if current_mode == "running":
            r.set("scheduler:mode", "draining")
            logger.warning("System entered DRAINING mode. Suspending new browser tasks.")
            
        # If lock is vacant and we are in draining mode, transition to offline and restart
        active_lock = r.get("lock:chrome_cdp_profile")
        if not active_lock and r.get("scheduler:mode") == "draining":
            r.set("scheduler:mode", "offline")
            logger.warning("System entered OFFLINE mode. Preparing browser maintenance.")
            await restart_chromium_process()
    else:
        if current_mode in ("draining", "offline"):
            r.set("scheduler:mode", "running")
            logger.info("System returned to RUNNING mode. Resuming normal operations.")

async def trigger_scheduled_tasks():
    """Polls SQLite for schedules due to run, pushes task payload, and updates next run time."""
    conn = get_db_conn()
    cursor = conn.cursor()
    now = datetime.now()
    
    try:
        # Fetch active schedules that are due
        schedules = cursor.execute(
            "SELECT * FROM cron_schedules WHERE is_active = 1 AND (next_run_at IS NULL OR next_run_at <= ?)",
            (now.isoformat(),)
        ).fetchall()
        
        for sch in schedules:
            task_id = f"task_cron_{sch['schedule_id']}_{int(now.timestamp())}"
            
            # Queue task into SQLite queue
            cursor.execute(
                """
                INSERT OR IGNORE INTO task_queue (task_id, source, agent_type, payload, status)
                VALUES (?, 'cron', ?, ?, 'pending')
                """,
                (task_id, sch['name'], sch['task_payload'])
            )
            
            # Calculate next trigger date
            new_next_run = croniter(sch['cron_expr'], now).get_next(datetime)
            cursor.execute(
                """
                UPDATE cron_schedules
                SET last_run_at = ?, next_run_at = ?
                WHERE schedule_id = ?
                """,
                (now.isoformat(), new_next_run.isoformat(), sch['schedule_id'])
            )
            logger.info(f"Cron triggered task {task_id} for schedule {sch['name']}. Next run: {new_next_run}")
            
        conn.commit()
    except Exception as e:
        logger.error(f"Error during cron trigger execution: {e}")
    finally:
        conn.close()

async def dispatch_queue_redis():
    """Dispatches pending tasks asynchronously, respecting Redis CDP lock constraints."""
    scheduler_mode = r.get("scheduler:mode") or "running"
    if scheduler_mode == "offline":
        return
        
    conn = get_db_conn()
    cursor = conn.cursor()
    
    try:
        pending_tasks = cursor.execute(
            "SELECT * FROM task_queue WHERE status = 'pending' ORDER BY created_at ASC"
        ).fetchall()
        
        for task in pending_tasks:
            task_id = task['task_id']
            agent_type = task['agent_type']
            
            if agent_type == 'finance':
                # Block browser tasks if draining
                if scheduler_mode == "draining":
                    continue
                    
                # Acquire Redis CDP lock
                lock_acquired = r.set("lock:chrome_cdp_profile", task_id, nx=True, ex=config.LOCK_TIMEOUT)
                if not lock_acquired:
                    continue  # Mutex locked, skip for now
                    
                logger.info(f"CDP lock acquired for task: {task_id}")
                cursor.execute(
                    "UPDATE task_queue SET status = 'running', updated_at = ? WHERE task_id = ?",
                    (datetime.now().isoformat(), task_id)
                )
                conn.commit()
                
                # Spawn task worker asynchronously
                asyncio.create_task(async_execute_task(task_id, use_chrome=True))
                
            else:
                # Developer / Scheduler tasks run concurrently without locks
                cursor.execute(
                    "UPDATE task_queue SET status = 'running', updated_at = ? WHERE task_id = ?",
                    (datetime.now().isoformat(), task_id)
                )
                conn.commit()
                
                asyncio.create_task(async_execute_task(task_id, use_chrome=False))
    except Exception as e:
        logger.error(f"Error while dispatching queue: {e}")
    finally:
        conn.close()

async def async_execute_task(task_id: str, use_chrome: bool):
    """
    Asynchronous executor loading the workflow router.
    Importing workflow router inside to avoid circular reference.
    """
    logger.info(f"Task {task_id} started execution.")
    try:
        from agents.workflow_router import run_workflow
        
        # Execute workflow
        result = await run_workflow(task_id)
        
        # Update SQLite status to completed
        update_task_status(task_id, 'completed', result_data=result)
        logger.info(f"Task {task_id} successfully completed.")
    except Exception as e:
        logger.error(f"Task {task_id} execution failed: {e}")
        update_task_status(task_id, 'failed', error_log=str(e))
    finally:
        # Unlock Redis if chrome resources were occupied
        if use_chrome:
            # Atomic unlock script
            unlock_script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            r.eval(unlock_script, 1, "lock:chrome_cdp_profile", task_id)
            logger.info(f"Released lock for task: {task_id}")

async def scheduler_loop():
    """Main daemon loop running both schedule triggers and task dispatchers."""
    logger.info("Scheduler daemon started.")
    while True:
        try:
            await check_and_update_scheduler_state()
            await trigger_scheduled_tasks()
            await dispatch_queue_redis()
        except Exception as e:
            logger.error(f"Error in scheduler heartbeat loop: {e}")
        await asyncio.sleep(10)

if __name__ == "__main__":
    # Setup standard stdout logging when running this module directly
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(scheduler_loop())
