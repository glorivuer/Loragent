import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
import sqlite3
from datetime import datetime
from croniter import croniter

# Setup environment overrides for testing
os.environ["DB_PATH"] = "test_state.db"

import config
import db.database as db

class TestHmsdkDatabase(unittest.TestCase):
    def setUp(self):
        # Override paths in database module for isolated testing
        db.DB_PATH = "test_state.db"
        config.DB_PATH = "test_state.db"
        
        # Initialize test database
        db.init_db()

    def tearDown(self):
        # Cleanup test DB file
        if os.path.exists("test_state.db"):
            try:
                os.remove("test_state.db")
            except Exception:
                pass
        # Clean WAL files
        for ext in ("-wal", "-journal", "-shm"):
            f = f"test_state.db{ext}"
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    def test_db_initialization(self):
        conn = db.get_db_conn()
        cursor = conn.cursor()
        
        # Verify tables exist
        tables = cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t['name'] for t in tables]
        
        self.assertIn("task_queue", table_names)
        self.assertIn("cron_schedules", table_names)
        self.assertIn("skills", table_names)
        conn.close()

    def test_task_lifecycle(self):
        task_id = "test_task_123"
        payload = {"test_key": "test_val"}
        
        # Add task
        self.assertTrue(db.add_task(task_id, "test_source", "developer", payload, 1111, 2222))
        
        # Retrieve task
        task = db.get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task['task_id'], task_id)
        self.assertEqual(task['source'], "test_source")
        self.assertEqual(task['agent_type'], "developer")
        self.assertEqual(task['payload'], payload)
        self.assertEqual(task['status'], "pending")
        self.assertEqual(task['tg_chat_id'], 1111)
        self.assertEqual(task['tg_msg_id'], 2222)
        
        # Update status to running
        self.assertTrue(db.update_task_status(task_id, "running"))
        task = db.get_task(task_id)
        self.assertEqual(task['status'], "running")
        
        # Complete task with results
        self.assertTrue(db.update_task_status(task_id, "completed", result_data="workflow result data"))
        task = db.get_task(task_id)
        self.assertEqual(task['status'], "completed")
        self.assertEqual(task['result_data'], "workflow result data")

    def test_cron_scheduling(self):
        schedule_id = "test_cron_123"
        name = "hourly_job"
        cron_expr = "0 * * * *"
        payload = {"job": "hourly"}
        
        # Add schedule
        self.assertTrue(db.add_cron_schedule(schedule_id, name, cron_expr, payload))
        
        # Verify next run was calculated and stored
        schedules = db.get_active_cron_schedules()
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0]['schedule_id'], schedule_id)
        self.assertEqual(schedules[0]['name'], name)
        self.assertEqual(schedules[0]['cron_expr'], cron_expr)
        self.assertEqual(schedules[0]['task_payload'], payload)
        self.assertIsNotNone(schedules[0]['next_run_at'])
        
        # Test update run dates
        now = datetime.now()
        next_run = croniter(cron_expr, now).get_next(datetime)
        self.assertTrue(db.update_cron_schedule_run(schedule_id, now, next_run))
        
        # Read the updated dates directly
        conn = db.get_db_conn()
        row = conn.execute("SELECT last_run_at, next_run_at FROM cron_schedules WHERE schedule_id = ?", (schedule_id,)).fetchone()
        conn.close()
        
        self.assertEqual(row['last_run_at'], now.isoformat())
        self.assertEqual(row['next_run_at'], next_run.isoformat())

    def test_dynamic_skills_registry(self):
        # Verify register_skill functionality
        skill_id = "env_abc_123"
        name = "test_fib"
        path = "/path/to/dynamic/test_fib.py"
        sandbox_env_id = "env_abc_123"
        
        self.assertTrue(db.register_skill(skill_id, name, path, sandbox_env_id))
        
        # Verify get_registered_skills
        skills = db.get_registered_skills()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]['skill_id'], skill_id)
        self.assertEqual(skills[0]['name'], name)
        self.assertEqual(skills[0]['path'], path)
        self.assertEqual(skills[0]['sandbox_env_id'], sandbox_env_id)

if __name__ == "__main__":
    unittest.main()
