import os
import json
import logging
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, ORCHESTRATOR_MODEL
from tools.antigravity_client import AntigravitySkillBuilder
from db.database import register_skill

logger = logging.getLogger(__name__)

# Target directory for dynamic skills
SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "dynamic")

async def run(payload: dict) -> str:
    """
    Developer Agent:
    1. Parse query to generate code and tests using Gemini.
    2. Submit code and tests to Antigravity Sandbox for compilation & unit test audit.
    3. If approved, download snapshot and extract verified files to local dynamic skills path.
    """
    query = payload.get("query", "")
    if not query:
        raise ValueError("No developer query provided.")
        
    logger.info(f"Developer Subagent: Generating code for query: {query}")
    
    # 1. Initialize Gemini Client to generate Python module and test cases
    client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()
    
    prompt = (
        f"You are a professional software engineer. Generate a Python module and its corresponding unit test (pytest) file "
        f"based on the following user requirement: '{query}'.\n\n"
        f"Format your response as a valid JSON object matching the following structure:\n"
        f"{{\n"
        f"  \"skill_name\": \"alphanumeric_skill_name_in_lowercase\",\n"
        f"  \"py_code\": \"complete python module source code including functions/classes with docstrings\",\n"
        f"  \"test_code\": \"complete pytest code containing multiple unit test cases\"\n"
        f"}}\n"
        f"Ensure all double quotes inside the python strings are properly escaped to maintain valid JSON."
    )
    
    # Request JSON response
    response = client.models.generate_content(
        model=ORCHESTRATOR_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    )
    
    try:
        data = json.loads(response.text)
        skill_name = data.get("skill_name", "temp_skill").strip().lower()
        py_code = data.get("py_code", "")
        test_code = data.get("test_code", "")
    except Exception as e:
        logger.error(f"Failed to parse JSON response from Gemini: {e}. Raw: {response.text}")
        raise RuntimeError(f"Gemini code generation failed to return valid JSON: {e}")
        
    logger.info(f"Generated skill '{skill_name}' code & tests. Submitting to Antigravity sandbox...")
    
    # 2. Run Antigravity Sandbox validator
    builder = AntigravitySkillBuilder()
    
    # This runs the multi-turn self-repair test loop
    env_id = builder.run_sandbox_testing(
        skill_name=skill_name,
        py_code=py_code,
        test_code=test_code,
        max_turns=3
    )
    
    # 3. Pull Sandbox snapshot and save locally
    os.makedirs(SKILLS_DIR, exist_ok=True)
    builder.download_skill_snapshot(env_id, SKILLS_DIR)
    
    # Verify that the downloaded file exists locally
    skill_file_path = os.path.join(SKILLS_DIR, f"{skill_name}.py")
    if os.path.exists(skill_file_path):
        logger.info(f"Skill file successfully saved to {skill_file_path}")
        register_skill(env_id, skill_name, skill_file_path, env_id)
        return (
            f"🚀 **Skill Created & Verified!**\n"
            f"📦 **Name**: `{skill_name}`\n"
            f"🛡️ **Sandbox Env**: `{env_id}`\n"
            f"📁 **Local Path**: `skills/dynamic/{skill_name}.py`"
        )
    else:
        logger.warning(f"Downloaded snapshot was unpacked, but skill file was not found at {skill_file_path}")
        # Try raw print download fallback
        try:
            fallback_code = builder.download_validated_code_text(env_id, "latest", skill_name)
            with open(skill_file_path, "w") as f:
                f.write(fallback_code)
            register_skill(env_id, skill_name, skill_file_path, env_id)
            return (
                f"🚀 **Skill Created via Text Fallback!**\n"
                f"📦 **Name**: `{skill_name}`\n"
                f"📁 **Local Path**: `skills/dynamic/{skill_name}.py`"
            )
        except Exception as fe:
            raise RuntimeError(f"Failed to extract verified skill code: {fe}")
