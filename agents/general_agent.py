import os
import json
import logging
import subprocess
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, ORCHESTRATOR_MODEL

logger = logging.getLogger(__name__)

# Workspace bounds
WORKSPACE_DIR = "/home/elvelyn/myapp"

def read_file(path: str) -> str:
    """Reads the contents of a file on the filesystem.
    
    Args:
        path: The path to the file to read (can be absolute or relative to workspace).
    """
    logger.info(f"Tool read_file called for path: {path}")
    abs_path = os.path.abspath(path if os.path.isabs(path) else os.path.join(WORKSPACE_DIR, path))
    if not abs_path.startswith(WORKSPACE_DIR):
        return f"Error: Permission denied. Access to path {path} is restricted to the workspace."
        
    if not os.path.exists(abs_path):
        return f"Error: File {path} not found."
    if os.path.isdir(abs_path):
        return f"Error: {path} is a directory. Use list_directory to view its contents."
        
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Truncate content to avoid model overflow (e.g. max 50KB)
        if len(content) > 50000:
            return content[:50000] + "\n\n[TRUNCATED DUE TO SIZE]"
        return content
    except Exception as e:
        return f"Error reading file {path}: {str(e)}"

def run_command(command: str) -> str:
    """Executes a terminal shell command on the host system.
    
    Args:
        command: The shell command to run (e.g. 'pytest tests/test_hmsdk.py').
    """
    logger.info(f"Tool run_command called for: {command}")
    # Simple block for safety
    blocked_keywords = ["rm -rf /", "mkfs", "dd ", "shutdown", "reboot", "sudo "]
    if any(k in command for k in blocked_keywords):
        return f"Error: Command contains blocked keywords for safety."
        
    try:
        # Run in Loragent directory
        cwd = os.path.join(WORKSPACE_DIR, "Loragent")
        res = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=cwd, timeout=60.0)
        output = f"Exit Code: {res.returncode}\n\nSTDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}"
        if len(output) > 30000:
            return output[:30000] + "\n\n[TRUNCATED]"
        return output
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 60 seconds."
    except Exception as e:
        return f"Error running command: {str(e)}"

def list_directory(path: str = ".") -> str:
    """Lists files and directories inside a directory path.
    
    Args:
        path: The directory path to list (default is current workspace root '.').
    """
    logger.info(f"Tool list_directory called for path: {path}")
    abs_path = os.path.abspath(path if os.path.isabs(path) else os.path.join(WORKSPACE_DIR, path))
    if not abs_path.startswith(WORKSPACE_DIR):
        return f"Error: Permission denied. Access to path {path} is restricted to the workspace."
        
    if not os.path.exists(abs_path):
        return f"Error: Path {path} not found."
    if not os.path.isdir(abs_path):
        return f"Error: {path} is not a directory. Use read_file to read it."
        
    try:
        items = os.listdir(abs_path)
        result = []
        for item in sorted(items):
            item_path = os.path.join(abs_path, item)
            is_dir = os.path.isdir(item_path)
            size = os.path.getsize(item_path) if not is_dir else 0
            type_str = "DIR " if is_dir else "FILE"
            size_str = f"{size} bytes" if not is_dir else ""
            result.append(f"[{type_str}] {item:<25} {size_str}")
        return "\n".join(result) or "[Empty Directory]"
    except Exception as e:
        return f"Error listing directory {path}: {str(e)}"

def delete_file(path: str) -> str:
    """Safely deletes a file from the local filesystem.
    Access is restricted to the workspace. Prevents deleting critical project configuration files.
    
    Args:
        path: The path of the file to delete (can be absolute or relative to workspace).
    """
    logger.info(f"Tool delete_file called for path: {path}")
    abs_path = os.path.abspath(path if os.path.isabs(path) else os.path.join(WORKSPACE_DIR, path))
    if not abs_path.startswith(WORKSPACE_DIR):
        return f"Error: Permission denied. Deleting files outside the workspace {WORKSPACE_DIR} is restricted."
        
    # Prevent deleting critical project config files
    filename = os.path.basename(abs_path)
    critical_files = [
        "state.db", "config.py", "main.py", ".env", "requirements.txt",
        "developer_agent.py", "general_agent.py", "finance_agent.py", "scheduler_agent.py",
        "workflow_router.py", "tg_bot.py", "handlers.py", "heartbeat.py"
    ]
    if filename in critical_files or ".git" in abs_path or "venv" in abs_path:
        return f"Error: Permission denied. Deleting critical system/project files ({filename}) is prohibited."
        
    if not os.path.exists(abs_path):
        return f"Error: File {path} not found."
    if os.path.isdir(abs_path):
        return f"Error: {path} is a directory. delete_file only deletes files. Use terminal commands for directories if needed."
        
    try:
        os.remove(abs_path)
        logger.info(f"Successfully deleted file: {abs_path}")
        return f"Success: File '{path}' has been deleted."
    except Exception as e:
        return f"Error deleting file {path}: {str(e)}"

async def run(payload: dict) -> str:
    """
    General Assistant Agent:
    Handles general tasks, question answering, and commands that require reading files
    or executing shell tasks using Gemini function calling.
    """
    query = payload.get("query", "")
    task_id = payload.get("task_id", "")
    
    if not query:
        raise ValueError("No query provided for general agent.")
        
    logger.info(f"General Subagent: Handling task: {query} (Task ID: {task_id})")
    
    # Define register_artifact with task_id closure
    def register_artifact(path: str) -> str:
        """Registers a local file or image path as an output artifact of the current task.
        This file will be automatically sent to the user on Telegram.
        
        Args:
            path: The path to the file or image to send (relative to workspace or absolute).
        """
        logger.info(f"Tool register_artifact called for path: {path} (Task: {task_id})")
        if not task_id:
            return "Error: No active task ID found. Cannot register artifact."
            
        abs_path = os.path.abspath(path if os.path.isabs(path) else os.path.join(WORKSPACE_DIR, path))
        if not abs_path.startswith(WORKSPACE_DIR):
            return f"Error: Permission denied. Access to path {path} is restricted to the workspace."
        if not os.path.exists(abs_path):
            return f"Error: File '{path}' not found."
        if os.path.isdir(abs_path):
            return f"Error: '{path}' is a directory. Only individual files can be registered as artifacts."
            
        try:
            from db.database import update_task_status
            update_task_status(task_id, "running", artifact_path=abs_path)
            logger.info(f"Successfully registered task {task_id} artifact path: {abs_path}")
            return f"Success: File '{path}' registered as task artifact and will be sent to you."
        except Exception as e:
            return f"Error registering artifact: {str(e)}"
            
    client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()
    
    prompt = (
        f"You are the General Assistant Subagent of the Hermes-ADK AI system.\n"
        f"Analyze and fulfill the user request by utilizing your tools (read_file, run_command, list_directory, delete_file, register_artifact).\n"
        f"You have access to the local project workspace under {WORKSPACE_DIR}.\n\n"
        f"User request: '{query}'\n\n"
        f"Instructions:\n"
        f"1. Work step-by-step. Read files, execute tests, list directories, and provide a clear, comprehensive report/response in Markdown format.\n"
        f"2. If the user asks to send them a file, download a video/media, or locate an image, you MUST call `register_artifact` with the file path after verifying the file exists on disk. This will automatically upload and deliver the file to the user.\n"
        f"3. If the user asks to delete a file, use the safe `delete_file` tool to remove it."
    )
    
    # Run the model with auto function calling
    response = client.models.generate_content(
        model=ORCHESTRATOR_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[read_file, run_command, list_directory, delete_file, register_artifact],
        )
    )
    
    return response.text
