import os
import json
import logging
import tempfile
import subprocess
import shutil
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, ORCHESTRATOR_MODEL
from tools.antigravity_client import AntigravitySkillBuilder
from db.database import register_skill

logger = logging.getLogger(__name__)

# Target directory for dynamic skills
SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "dynamic")

def classify_sandbox_requirement(query: str, client, model: str) -> bool:
    """
    Determines if the developer request needs a cloud sandbox or can be run locally.
    Defaults to local execution (False) for speed and stability, unless there is
    a clear security risk or heavy system-level requirements that could impact the host.
    """
    logger.info(f"Classifying sandbox requirement for query: {query}")
    prompt = (
        f"Analyze this requirement for creating a new python script/skill:\n"
        f"'{query}'\n\n"
        f"Determine if this task requires a remote hosted execution/verification sandbox. "
        f"Rules for classification:\n"
        f"1. DEFAULT is false (local execution is preferred for speed, simplicity, and reliability).\n"
        f"2. Set to true ONLY if there is a major security risk (e.g. executing unsafe commands that modify critical system configurations, "
        f"large-scale deletion/writing to system files outside the workspace) or if it requires heavy, OS-level dependencies "
        f"or browser automation (like Playwright/Selenium/CDP chrome profiling) that cannot easily run in a standard python local venv.\n"
        f"3. Tasks that merely use standard libraries, simple API requests, yt-dlp, or parsing should run locally (sandbox_required: false).\n\n"
        f"Return a JSON object in this format:\n"
        f"{{\n"
        f"  \"sandbox_required\": true | false,\n"
        f"  \"reason\": \"explanation of why sandbox is or is not required\"\n"
        f"}}\n"
    )
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        data = json.loads(response.text)
        req = data.get("sandbox_required", False)
        logger.info(f"Sandbox classification result: {req} (Reason: {data.get('reason')})")
        return req
    except Exception as e:
        logger.warning(f"Failed to classify sandbox requirement using Gemini: {e}. Defaulting to local execution for speed and stability.")
        return False

async def run_local_tests(skill_name: str, py_code: str, test_code: str, max_turns: int = 3, client = None) -> tuple[bool, str, str, str]:
    """
    Validates code and tests locally in a temporary directory using local pytest.
    Performs self-repair up to max_turns on failure.
    Returns (success, final_py_code, final_test_code, log_message)
    """
    logger.info(f"Initiating local testing for skill: {skill_name}...")
    
    # Create temp dir within workspace to obey permissions
    workspace_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    temp_dir = tempfile.mkdtemp(dir=workspace_dir, prefix="tmp_test_")
    
    try:
        src_dir = os.path.join(temp_dir, "src")
        tests_dir = os.path.join(temp_dir, "tests")
        os.makedirs(src_dir, exist_ok=True)
        os.makedirs(tests_dir, exist_ok=True)
        
        # Write initial files and touch __init__.py files
        with open(os.path.join(src_dir, "__init__.py"), "w") as f:
            pass
        with open(os.path.join(tests_dir, "__init__.py"), "w") as f:
            pass
            
        turn = 1
        current_py = py_code
        current_test = test_code
        
        # Run self-repair loop
        while turn <= max_turns:
            py_file = os.path.join(src_dir, f"{skill_name}.py")
            test_file = os.path.join(tests_dir, f"test_{skill_name}.py")
            
            with open(py_file, "w") as f:
                f.write(current_py)
            with open(test_file, "w") as f:
                f.write(current_test)
                
            # Run pytest using local venv pytest
            pytest_path = os.path.join(workspace_dir, "venv", "bin", "pytest")
            # Set PYTHONPATH so tests can import src
            env = os.environ.copy()
            env["PYTHONPATH"] = f"{temp_dir}:{src_dir}"
            
            res = subprocess.run(
                [pytest_path, test_file],
                capture_output=True,
                text=True,
                env=env
            )
            
            output = res.stdout + "\n" + res.stderr
            logger.info(f"Local pytest output (Turn {turn}):\n{output}")
            
            if res.returncode == 0:
                logger.info(f"Local verification successfully passed on turn {turn}.")
                return True, current_py, current_test, f"Passed on turn {turn}"
                
            if turn == max_turns:
                logger.error("Local testing reached maximum self-repair turns without resolving errors.")
                return False, current_py, current_test, f"Failed after {max_turns} turns. Error: {output}"
                
            logger.warning(f"Local test failed. Initiating self-repair (Turn {turn}/{max_turns})...")
            
            # Request self-repair from Gemini
            prompt = (
                f"You generated a Python module and a pytest file, but the tests failed with the following output:\n\n"
                f"--- PYTEST OUTPUT ---\n{output}\n---------------------\n\n"
                f"Here is the code you generated:\n"
                f"Module code:\n```python\n{current_py}\n```\n"
                f"Test code:\n```python\n{current_test}\n```\n\n"
                f"Analyze the failures, fix the errors, and output the updated module and test code."
            )
            
            schema = {
                "type": "OBJECT",
                "properties": {
                    "skill_name": {"type": "STRING"},
                    "py_code": {"type": "STRING"},
                    "test_code": {"type": "STRING"}
                },
                "required": ["skill_name", "py_code", "test_code"]
            }
            
            response = client.models.generate_content(
                model=ORCHESTRATOR_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema
                )
            )
            
            try:
                data = json.loads(response.text)
                current_py = data.get("py_code", current_py)
                current_test = data.get("test_code", current_test)
            except Exception as re_err:
                logger.error(f"Failed to parse repair JSON: {re_err}. Raw: {response.text}")
                return False, current_py, current_test, f"Repair failed: JSON parse error {re_err}"
                
            turn += 1
            
        return False, current_py, current_test, "Unreachable"
        
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

def generate_skill_description(skill_name: str, py_code: str, client, model: str) -> str:
    """Generates a brief description for the skill using Gemini."""
    prompt = (
        f"Given the following Python code for a skill named '{skill_name}':\n\n"
        f"```python\n{py_code}\n```\n\n"
        f"Generate a concise 1-2 sentence description of what this skill does."
    )
    try:
        response = client.models.generate_content(model=model, contents=prompt)
        return response.text.strip().replace('"', '\\"')
    except Exception:
        return f"A custom dynamic skill generated for {skill_name}."

def write_skill_playbook(skill_name: str, py_code: str, test_code: str, description: str, playbook_md: str = None) -> str:
    """
    Writes the skill's SKILL.md playbook and its scripts to skills/dynamic/skill_name/
    Returns the absolute path to the SKILL.md file.
    """
    skill_folder = os.path.join(SKILLS_DIR, skill_name)
    scripts_dir = os.path.join(skill_folder, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    
    # Write python code and tests
    py_path = os.path.join(scripts_dir, f"{skill_name}.py")
    test_path = os.path.join(scripts_dir, f"test_{skill_name}.py")
    
    with open(py_path, "w", encoding="utf-8") as f:
        f.write(py_code)
    with open(test_path, "w", encoding="utf-8") as f:
        f.write(test_code)
        
    # Write SKILL.md
    skill_md_path = os.path.join(skill_folder, "SKILL.md")
    
    if not playbook_md:
        playbook_md = (
            f"---\n"
            f"name: {skill_name}\n"
            f"description: \"{description}\"\n"
            f"version: 1.0.0\n"
            f"author: Developer Agent\n"
            f"license: MIT\n"
            f"platforms: [linux, macos, windows]\n"
            f"---\n\n"
            f"# {skill_name}\n\n"
            f"{description}\n\n"
            f"## Usage\n"
            f"This skill can be executed by running the script at:\n"
            f"```bash\n"
            f"python skills/dynamic/{skill_name}/scripts/{skill_name}.py\n"
            f"```\n\n"
            f"## Verification Tests\n"
            f"Unit tests are located in `skills/dynamic/{skill_name}/scripts/test_{skill_name}.py`.\n"
            f"You can verify the tests locally by running:\n"
            f"```bash\n"
            f"pytest skills/dynamic/{skill_name}/scripts/test_{skill_name}.py\n"
            f"```\n"
        )
    
    with open(skill_md_path, "w", encoding="utf-8") as f:
        f.write(playbook_md)
        
    logger.info(f"Saved Markdown playbook skill to {skill_md_path}")
    return skill_md_path

def compile_project_context() -> str:
    """Compiles Loragent project context to inject into the Developer Agent's prompt."""
    import os
    from db.database import get_all_skills
    
    # 1. Fetch available skills
    skills_ctx = ""
    try:
        skills = get_all_skills()
        if skills:
            skills_ctx = "\n".join([f"- {s['name']}: {s.get('description', 'No description.')}" for s in skills])
    except Exception as e:
        skills_ctx = f"Error fetching skills: {e}"
        
    # 2. Project structure layout
    project_layout = (
        "- agents/developer_agent.py: Handles skill development & verification\n"
        "- agents/general_agent.py: Handles general tasks, safe deletions, and local inspection\n"
        "- agents/workflow_router.py: Routes tasks to specialized agents\n"
        "- gateway/tg_bot.py: Handles Telegram bot gateway, polling, and media/artifact uploads\n"
        "- db/database.py: Database schema, operations, and registered skill mappings\n"
        "- skills/static/: Pre-installed static playbooks (like claude-code)\n"
        "- skills/dynamic/: Dynamically generated playbooks and scripts\n"
    )
    
    # 3. DB Schema details
    db_schema = (
        "Table: task_queue (task_id TEXT, source TEXT, status TEXT, payload TEXT, result_data TEXT, artifact_path TEXT, error_log TEXT)\n"
        "Table: skills (skill_id TEXT, name TEXT, path TEXT, sandbox_env_id TEXT)\n"
        "Table: cron_schedules (schedule_id TEXT, name TEXT, cron_expr TEXT, task_payload TEXT, is_active INTEGER)\n"
    )
    
    # 4. Installed Python dependencies
    dependencies = "google-genai, playwright, redis, python-telegram-bot, croniter, httpx, pytest, yt-dlp"
    
    context = (
        f"=== PROJECT CONTEXT ===\n"
        f"Project Name: Loragent (Hermes-ADK 2.0)\n"
        f"Directory Layout:\n{project_layout}\n"
        f"Database Schema:\n{db_schema}\n"
        f"Pre-installed local Python packages: {dependencies}\n"
        f"Already Registered Skills:\n{skills_ctx or 'None'}\n"
        f"======================="
    )
    return context

async def run(payload: dict) -> str:
    """
    Developer Agent:
    1. Parse query to generate code and tests using Gemini.
    2. Classify if query requires remote sandbox.
    3. Run local or remote pytest self-repair testing.
    4. Write a Markdown playbook SKILL.md and scripts.
    5. Register the SKILL.md path in SQLite.
    """
    query = payload.get("query", "")
    if not query:
        raise ValueError("No developer query provided.")
        
    # 1. Initialize Gemini Client
    client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()
    
    # Check if the user is asking to execute/run/use an existing skill
    exec_prompt = (
        f"Analyze this request: '{query}'\n\n"
        f"Determine if the user is asking to execute/run/use an already existing skill, or if they are asking to write/create/develop a new skill.\n"
        f"Return a JSON object in this format:\n"
        f"{{\n"
        f"  \"is_execution\": true | false,\n"
        f"  \"skill_name\": \"name of the skill to execute (lowercase, snake_case/slug, if is_execution is true)\",\n"
        f"  \"args\": \"arguments string to pass to the script (if is_execution is true, else empty)\"\n"
        f"}}\n"
    )
    try:
        exec_res = client.models.generate_content(
            model=ORCHESTRATOR_MODEL,
            contents=exec_prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        exec_data = json.loads(exec_res.text)
        if exec_data.get("is_execution", False):
            target_skill = exec_data.get("skill_name", "").strip().lower()
            args_str = exec_data.get("args", "").strip()
            
            logger.info(f"Detected request to execute skill '{target_skill}' with args: {args_str}")
            
            # Lookup skill in SQLite
            from db.database import get_all_skills
            all_skills = get_all_skills()
            matched_skill = None
            for s in all_skills:
                if s['name'] == target_skill:
                    matched_skill = s
                    break
            
            if not matched_skill:
                return f"❌ **Error**: Dynamic skill `{target_skill}` is not registered in the database. Please list available skills with `/skills`."
                
            skill_md_path = matched_skill['path']
            skill_folder = os.path.dirname(skill_md_path)
            py_path = os.path.join(skill_folder, "scripts", f"{target_skill}.py")
            
            if not os.path.exists(py_path):
                return f"❌ **Error**: Script file for skill `{target_skill}` was not found at `{py_path}`."
                
            workspace_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            python_path = os.path.join(workspace_dir, "venv", "bin", "python")
            
            # Split args appropriately if present
            cmd = [python_path, py_path]
            if args_str:
                import shlex
                cmd.extend(shlex.split(args_str))
                
            logger.info(f"Running command: {' '.join(cmd)}")
            res = subprocess.run(cmd, capture_output=True, text=True, cwd=workspace_dir)
            
            if res.returncode == 0:
                return (
                    f"✅ **Skill Executed Successfully!**\n"
                    f"📦 **Skill**: `{target_skill}`\n"
                    f"📥 **Arguments**: `{args_str or 'None'}`\n\n"
                    f"📊 **Output**:\n```\n{res.stdout.strip()}\n```"
                )
            else:
                return (
                    f"❌ **Skill Execution Failed!**\n"
                    f"📦 **Skill**: `{target_skill}`\n"
                    f"📥 **Arguments**: `{args_str or 'None'}`\n"
                    f"⚠️ **Exit Code**: `{res.returncode}`\n\n"
                    f"🔴 **Error Output**:\n```\n{res.stderr.strip() or res.stdout.strip()}\n```"
                )
    except Exception as exec_ex:
        logger.warning(f"Failed to check/execute skill: {exec_ex}. Proceeding to skill creation flow...")

    # Load general agent's file tools locally to avoid circular dependencies
    from agents.general_agent import read_file

    logger.info(f"Developer Subagent: Generating code for query: {query}")
    
    project_context = compile_project_context()
    
    prompt = (
        f"You are a professional software engineer. Generate a Python module, its corresponding unit test (pytest) file, "
        f"and a comprehensive markdown playbook (SKILL.md) based on the following user requirement: '{query}'.\n\n"
        f"You are equipped with a workspace inspection tool (`read_file`). If you need to inspect existing files, "
        f"database helper functions, configurations, or skills to align your generated script with the project architecture, "
        f"you should call this tool before outputting the final JSON response. "
        f"Keep your tool calls minimal (1-2 calls max) to inspect only the target files you need. Do not try to explore the directory structure using tools.\n\n"
        f"{project_context}\n\n"
        f"Follow these strict design guidelines:\n\n"
        f"1. CLI Interface Design (Best Practices):\n"
        f"   - The Python script must be executable from the command line using `argparse` or `sys.argv`.\n"
        f"   - It must support a non-interactive print mode (exiting immediately after printing results to stdout).\n"
        f"   - It must support a `--json` command-line flag. When passed, it must output results as a valid JSON object to stdout.\n"
        f"   - If the task involves processing input files or streams, it should support reading from standard input (stdin) when `-` is passed or when input is piped.\n"
        f"   - It must return exit code 0 on success, and a non-zero exit code (e.g., 1 or 2) on error/failure.\n"
        f"   - Implement robust error handling so that errors are caught, detailed messages are written to stderr, and it exits cleanly with a non-zero code.\n"
        f"   - For local execution compatibility, prefer Python's built-in standard libraries (e.g. `urllib`, `json`, `argparse`, `sys`, `os`, `re`, `subprocess`, `math`) over heavy third-party dependencies unless absolutely necessary.\n\n"
        f"2. Playbook (SKILL.md) Documentation Design (modeled after Claude Code documentation pattern):\n"
        f"   - Generate a markdown playbook containing metadata in YAML frontmatter, detailed Prerequisites (env variables, packages), "
        f"Orchestration modes, a CLI Flags reference table, Piping Input instructions, structured JSON output details, Pitfalls & Gotchas, and Verification instructions.\n\n"
        f"3. Code Simplicity & Verification Guidelines (Karpathy Guidelines):\n"
        f"   - Simplicity First: Write the minimum code that solves the problem. Nothing speculative. No single-use abstractions, and no unrequested configurability.\n"
        f"   - Surgical Changes: Touch only what you must. Remove any variables/imports/functions that your changes make unused.\n"
        f"   - Goal-Driven Execution: Focus on writing strict, verifiable pytest test cases, including edge cases and invalid inputs, to confirm success.\n\n"
        f"Format your response as a valid JSON object."
    )
    
    schema = {
        "type": "OBJECT",
        "properties": {
            "skill_name": {"type": "STRING"},
            "py_code": {"type": "STRING"},
            "test_code": {"type": "STRING"},
            "playbook_md": {"type": "STRING"}
        },
        "required": ["skill_name", "py_code", "test_code", "playbook_md"]
    }
    
    # Request JSON response
    response = client.models.generate_content(
        model=ORCHESTRATOR_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[read_file],
            response_mime_type="application/json",
            response_schema=schema
        )
    )
    
    try:
        data = json.loads(response.text)
        skill_name = data.get("skill_name", "temp_skill").strip().lower()
        py_code = data.get("py_code", "")
        test_code = data.get("test_code", "")
        playbook_md = data.get("playbook_md", "")
    except Exception as e:
        logger.error(f"Failed to parse JSON response from Gemini: {e}. Raw: {response.text}")
        raise RuntimeError(f"Gemini code generation failed to return valid JSON: {e}")
        
    # 2. Check if sandbox is required
    sandbox_required = classify_sandbox_requirement(query, client, ORCHESTRATOR_MODEL)
    
    description = generate_skill_description(skill_name, py_code, client, ORCHESTRATOR_MODEL)
    env_id = "local"
    
    if not sandbox_required:
        # 3a. Run local test-repair loop
        success, final_py, final_test, log_msg = await run_local_tests(skill_name, py_code, test_code, max_turns=3, client=client)
        if not success:
            raise RuntimeError(f"Local skill verification and self-repair failed: {log_msg}")
        py_code, test_code = final_py, final_test
    else:
        # 3b. Run Antigravity Sandbox validator
        logger.info(f"Sandbox required. Submitting skill '{skill_name}' to Antigravity sandbox...")
        builder = AntigravitySkillBuilder()
        env_id = builder.run_sandbox_testing(
            skill_name=skill_name,
            py_code=py_code,
            test_code=test_code,
            max_turns=3
        )
        # Pull Sandbox snapshot and save to temp location to fetch validated files
        temp_unpack_dir = os.path.join(SKILLS_DIR, f"unpack_{env_id}")
        os.makedirs(temp_unpack_dir, exist_ok=True)
        try:
            builder.download_skill_snapshot(env_id, temp_unpack_dir)
            skill_file_path = os.path.join(temp_unpack_dir, f"{skill_name}.py")
            if os.path.exists(skill_file_path):
                with open(skill_file_path, "r", encoding="utf-8") as f:
                    py_code = f.read()
            else:
                # Text fallback if snapshot tar lacks the file
                py_code = builder.download_validated_code_text(env_id, "latest", skill_name)
        finally:
            shutil.rmtree(temp_unpack_dir, ignore_errors=True)
 
    # 4. Write Markdown playbook and scripts
    os.makedirs(SKILLS_DIR, exist_ok=True)
    skill_md_path = write_skill_playbook(skill_name, py_code, test_code, description, playbook_md)
    
    # Register in database using SKILL.md path
    register_skill(env_id, skill_name, skill_md_path, env_id)
    
    mode_str = "☁️ **Antigravity Sandbox**" if sandbox_required else "💻 **Local Verification**"
    return (
        f"🚀 **Skill Created & Verified!**\n"
        f"📦 **Name**: `{skill_name}`\n"
        f"🛠️ **Validation Mode**: {mode_str}\n"
        f"📁 **Playbook Path**: `skills/dynamic/{skill_name}/SKILL.md`\n"
        f"📝 **Description**: {description}"
    )
