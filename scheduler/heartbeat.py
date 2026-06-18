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

async def restart_chrome_process():
    """Restarts host Chrome to clear memory leaks and bloated cache."""
    logger.warning("Initiating physical Chrome restart...")
    try:
        # Kill both Chrome and Chromium debugging processes
        kill_chrome = await asyncio.create_subprocess_shell("pkill -f 'chrome|chromium'")
        await kill_chrome.wait()
        logger.info("Chrome processes terminated.")
        
        # Give OS a moment to release resources
        await asyncio.sleep(2)
        
        # Re-launch Chrome under remote debugging port
        cmd = (
            "google-chrome --no-sandbox --disable-gpu --remote-debugging-port=9222 "
            "--user-data-dir=/home/elvelyn/myapp/Lor_profile/elvynchou_profile "
            "--no-first-run --no-default-browser-check &"
        )
        launch = await asyncio.create_subprocess_shell(cmd)
        await launch.wait()
        
        # Allow browser time to initialize
        await asyncio.sleep(5)
        logger.info("Chrome remote debugger restarted successfully on port 9222.")
    except Exception as e:
        logger.error(f"Failed to restart Chrome debugger: {e}")

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
            await restart_chrome_process()
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

async def async_run_background_review(task_id: str):
    """
    Background review worker:
    1. Audits a completed or failed task.
    2. Invokes Gemini as a Curator Agent to see if a skill needs to be created or patched.
    3. If yes, runs local tests and registers the new/updated skill.
    4. Sends a notification to Telegram if a skill was created/updated.
    """
    import json
    import uuid
    import os
    logger.info(f"Starting background Curator review for task {task_id}...")
    await asyncio.sleep(2)  # Let database states settle
    
    from db.database import get_task, register_skill
    task = get_task(task_id)
    if not task:
        return
        
    agent_type = task.get("agent_type")
    # Avoid infinite loop: don't review tasks generated by review itself
    if task_id.startswith("task_review_"):
        return
        
    payload = task.get("payload")
    status = task.get("status")
    result_data = task.get("result_data") or ""
    error_log = task.get("error_log") or ""
    tg_chat_id = task.get("tg_chat_id")
    
    # Initialize GenAI client
    from google import genai
    from google.genai import types
    
    client = genai.Client(api_key=config.GEMINI_API_KEY) if config.GEMINI_API_KEY else genai.Client()
    
    review_prompt = (
        "You are the Curator Agent (Self-Improvement Reviewer) of the Hermes-ADK AI system.\n"
        "Your job is to audit completed or failed tasks and decide if we need to create a new dynamic skill "
        "or update an existing one to automate this workflow or prevent similar errors in the future.\n\n"
        "Adhere to these strict design and coding guidelines when creating/updating skills:\n\n"
        "1. CLI Interface Design:\n"
        "   - The Python script must be executable from the command line using `argparse` or `sys.argv`.\n"
        "   - It must support a non-interactive print mode (exiting immediately after printing results to stdout).\n"
        "   - It must support a `--json` command-line flag. When passed, it must output results as a valid JSON object to stdout.\n"
        "   - If the task involves processing input files or streams, it should support reading from standard input (stdin) when `-` is passed or when input is piped.\n"
        "   - It must return exit code 0 on success, and a non-zero exit code (e.g., 1 or 2) on error/failure.\n"
        "   - Implement robust error handling so that errors are caught, detailed messages are written to stderr, and it exits cleanly with a non-zero code.\n"
        "   - For local execution compatibility, prefer Python's built-in standard libraries (e.g. `urllib`, `json`, `argparse`, `sys`, `os`, `re`, `subprocess`, `math`) over heavy third-party dependencies unless absolutely necessary.\n\n"
        "2. Playbook (SKILL.md) Documentation Design (modeled after Claude Code documentation pattern):\n"
        "   - Generate a markdown playbook containing metadata in YAML frontmatter, detailed Prerequisites (env variables, packages), "
        "Orchestration modes, a CLI Flags reference table, Piping Input instructions, structured JSON output details, Pitfalls & Gotchas, and Verification instructions.\n\n"
        "3. Code Simplicity & Verification Guidelines (Karpathy Guidelines):\n"
        "   - Simplicity First: Propose the absolute minimum code that solves the problem. Avoid overcomplication, single-use abstractions, and speculative configurability.\n"
        "   - Surgical Changes: Touch only what is necessary. Maintain existing coding style, and remove any variables/imports/functions that your changes make unused.\n"
        "   - Goal-Driven Verification: Propose comprehensive pytest tests covering invalid inputs, edge cases, and success paths to verify correctness.\n\n"
        f"--- TASK DETAILS ---\n"
        f"Task ID: {task_id}\n"
        f"Agent Type: {agent_type}\n"
        f"Payload: {json.dumps(payload)}\n"
        f"Execution Status: {status}\n"
        f"Result Data: {result_data[:5000]}\n"
        f"Error Log: {error_log}\n"
        f"--------------------\n\n"
        "Analyze the task. If this was a programming request, a complex workflow correction, or a debugging path, "
        "should we create/patch a dynamic skill? (Only do this for substantial scripts/helpers, not for simple one-off questions).\n\n"
        "Reply in a strict JSON format."
    )
    
    schema = {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING"},
            "skill_name": {"type": "STRING"},
            "description": {"type": "STRING"},
            "py_code": {"type": "STRING"},
            "test_code": {"type": "STRING"},
            "playbook_md": {"type": "STRING"}
        },
        "required": ["action", "skill_name", "description", "py_code", "test_code", "playbook_md"]
    }
    
    try:
        response = client.models.generate_content(
            model=config.ORCHESTRATOR_MODEL,
            contents=review_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema
            )
        )
        data = json.loads(response.text)
        action = data.get("action", "nothing")
        if action in ("create", "update"):
            skill_name = data.get("skill_name", "").strip().lower()
            py_code = data.get("py_code", "")
            test_code = data.get("test_code", "")
            description = data.get("description", "")
            playbook_md = data.get("playbook_md", "")
            
            if not skill_name or not py_code:
                logger.info("Curator decided to create skill, but missing name/code.")
                return
                
            logger.info(f"Curator Agent: Proposing to {action} skill '{skill_name}'. Verifying locally...")
            
            from agents.developer_agent import run_local_tests, write_skill_playbook
            
            # Verify the skill code locally
            success, final_py, final_test, log_msg = await run_local_tests(
                skill_name, py_code, test_code, max_turns=3, client=client
            )
            
            if success:
                # Save the verified playbook
                skill_md_path = write_skill_playbook(skill_name, final_py, final_test, description, playbook_md)
                register_skill(skill_name, skill_name, skill_md_path, "curator_review")
                
                logger.info(f"Curator successfully registered skill: {skill_name}")
                
                # Notify user by queuing a Telegram notification message task
                if tg_chat_id:
                    notify_task_id = f"task_review_notify_{uuid.uuid4().hex[:6]}"
                    conn = get_db_conn()
                    cursor = conn.cursor()
                    
                    notify_text = (
                        f"🤖 **Curator Agent Self-Improvement**:\n"
                        f"While auditing task `{task_id}`, I identified a learning opportunity and successfully "
                        f"**{action}d** dynamic skill `{skill_name}`!\n"
                        f"📝 **Description**: {description}\n"
                        f"📁 **Path**: `skills/dynamic/{skill_name}/SKILL.md`"
                    )
                    
                    cursor.execute(
                        """
                        INSERT INTO task_queue (task_id, source, agent_type, payload, status, result_data, tg_chat_id, tg_msg_id, tg_notified_status)
                        VALUES (?, 'review', 'curator', '{}', 'completed', ?, ?, -1, 'pending')
                        """,
                        (notify_task_id, notify_text, tg_chat_id)
                    )
                    conn.commit()
                    conn.close()
            else:
                logger.warning(f"Curator proposed skill '{skill_name}' failed local validation: {log_msg}")
    except Exception as ex:
        logger.error(f"Error in background Curator review: {ex}")

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
        # Trigger background Curator review task
        asyncio.create_task(async_run_background_review(task_id))
        
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
