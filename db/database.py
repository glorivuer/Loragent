import sqlite3
import json
import logging
from datetime import datetime
from croniter import croniter
from config import DB_PATH

logger = logging.getLogger(__name__)

def get_db_conn() -> sqlite3.Connection:
    """
    Establish a connection to the SQLite database.
    Enforces Row factory, WAL mode, and foreign keys.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # Enable Write-Ahead Logging (WAL) for safe concurrent reads/writes
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception as e:
        logger.error(f"Error configuring SQLite connection: {e}")
    return conn

def init_db():
    """
    Initialize SQLite database and create schemas if they do not exist.
    """
    conn = get_db_conn()
    cursor = conn.cursor()
    
    # 1. Create task_queue table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS task_queue (
        task_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        tg_chat_id INTEGER,
        tg_msg_id INTEGER,
        agent_type TEXT NOT NULL,
        payload TEXT,
        status TEXT CHECK(status IN ('pending', 'running', 'paused', 'completed', 'failed')) DEFAULT 'pending',
        session_id TEXT,
        result_data TEXT,
        artifact_path TEXT,
        error_log TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    
    # 2. Create cron_schedules table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cron_schedules (
        schedule_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        cron_expr TEXT NOT NULL,
        task_payload TEXT NOT NULL,
        is_active INTEGER DEFAULT 1,
        last_run_at TIMESTAMP,
        next_run_at TIMESTAMP
    );
    """)
    
    # 3. Create skills table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS skills (
        skill_id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        path TEXT NOT NULL,
        sandbox_env_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    
    conn.commit()
    conn.close()
    logger.info("SQLite database initialized successfully.")

# Helper Functions for Tasks
def add_task(task_id: str, source: str, agent_type: str, payload: dict, tg_chat_id: int = None, tg_msg_id: int = None) -> bool:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        payload_str = json.dumps(payload)
        cursor.execute(
            """
            INSERT INTO task_queue (task_id, source, agent_type, payload, tg_chat_id, tg_msg_id, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (task_id, source, agent_type, payload_str, tg_chat_id, tg_msg_id)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Failed to add task {task_id}: {e}")
        return False

def get_task(task_id: str) -> dict:
    conn = get_db_conn()
    cursor = conn.cursor()
    row = cursor.execute("SELECT * FROM task_queue WHERE task_id = ?", (task_id,)).fetchone()
    conn.close()
    if row:
        res = dict(row)
        if res.get('payload'):
            res['payload'] = json.loads(res['payload'])
        return res
    return None

def update_task_status(task_id: str, status: str, result_data: str = None, error_log: str = None, artifact_path: str = None) -> bool:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        now_str = datetime.now().isoformat()
        
        # Build dynamic query to avoid overwriting unchanged fields with NULL
        updates = ["status = ?", "updated_at = ?"]
        params = [status, now_str]
        
        if result_data is not None:
            updates.append("result_data = ?")
            params.append(result_data)
        if error_log is not None:
            updates.append("error_log = ?")
            params.append(error_log)
        if artifact_path is not None:
            updates.append("artifact_path = ?")
            params.append(artifact_path)
            
        params.append(task_id)
        query = f"UPDATE task_queue SET {', '.join(updates)} WHERE task_id = ?"
        
        cursor.execute(query, tuple(params))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Failed to update task {task_id} to {status}: {e}")
        return False

# Helper Functions for Cron Schedules
def add_cron_schedule(schedule_id: str, name: str, cron_expr: str, task_payload: dict, is_active: int = 1) -> bool:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        
        # Calculate the first next_run_at relative to current time
        now = datetime.now()
        next_run = croniter(cron_expr, now).get_next(datetime).isoformat()
        
        payload_str = json.dumps(task_payload)
        cursor.execute(
            """
            INSERT OR REPLACE INTO cron_schedules (schedule_id, name, cron_expr, task_payload, is_active, next_run_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (schedule_id, name, cron_expr, payload_str, is_active, next_run)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Failed to add cron schedule {schedule_id}: {e}")
        return False

def get_active_cron_schedules() -> list:
    conn = get_db_conn()
    cursor = conn.cursor()
    rows = cursor.execute("SELECT * FROM cron_schedules WHERE is_active = 1").fetchall()
    conn.close()
    
    schedules = []
    for r in rows:
        sch = dict(r)
        sch['task_payload'] = json.loads(sch['task_payload'])
        schedules.append(sch)
    return schedules

def update_cron_schedule_run(schedule_id: str, last_run_at: datetime, next_run_at: datetime) -> bool:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        
        last_run_str = last_run_at.isoformat() if isinstance(last_run_at, datetime) else last_run_at
        next_run_str = next_run_at.isoformat() if isinstance(next_run_at, datetime) else next_run_at
        
        cursor.execute(
            """
            UPDATE cron_schedules
            SET last_run_at = ?, next_run_at = ?
            WHERE schedule_id = ?
            """,
            (last_run_str, next_run_str, schedule_id)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Failed to update cron run for schedule {schedule_id}: {e}")
        return False

def register_skill(skill_id: str, name: str, path: str, sandbox_env_id: str = None) -> bool:
    """Inserts or replaces a verified dynamic skill in the registry database."""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO skills (skill_id, name, path, sandbox_env_id)
            VALUES (?, ?, ?, ?)
            """,
            (skill_id, name, path, sandbox_env_id)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Failed to register skill {name} in SQLite: {e}")
        return False

def get_registered_skills() -> list:
    """Fetches all registered dynamic skills sorted by name."""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        rows = cursor.execute("SELECT * FROM skills ORDER BY name ASC").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Failed to fetch registered skills: {e}")
        return []
