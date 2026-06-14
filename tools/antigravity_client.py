import os
import re
import httpx
import tarfile
import logging
from google import genai
from config import GEMINI_API_KEY, ANTIGRAVITY_MODEL

logger = logging.getLogger(__name__)

class AntigravitySkillBuilder:
    def __init__(self):
        # Initialize client with specified API key
        if GEMINI_API_KEY:
            self.client = genai.Client(api_key=GEMINI_API_KEY)
        else:
            # Fallback to default search for GEMINI_API_KEY in env variables
            self.client = genai.Client()
        self.model_agent = ANTIGRAVITY_MODEL

    def run_sandbox_testing(self, skill_name: str, py_code: str, test_code: str, max_turns: int = 3) -> str:
        """
        Runs compilation, audit, and verification inside a Google-managed remote sandbox.
        Performs up to max_turns self-repair attempts on test failures.
        Returns the environment ID of the successful run.
        """
        logger.info(f"Initiating remote sandbox testing for skill: {skill_name}...")
        
        # 1. Provision remote sandbox and write source & test files
        interaction = self.client.interactions.create(
            agent=self.model_agent,
            environment={"type": "remote"},
            input=f"Write two files inside the sandbox:\n"
                  f"1. `src/{skill_name}.py` with this content:\n{py_code}\n\n"
                  f"2. `tests/test_{skill_name}.py` with this content:\n{test_code}\n"
                  f"Then run pytest to ensure all test cases pass.",
            system_instruction="You are a strict security auditor. Run tests and verify the code doesn't contain exploits."
        )
        
        env_id = interaction.environment_id
        last_id = interaction.id
        turn = 1
        
        # 2. Self-repair loop
        while turn <= max_turns:
            output = interaction.output_text
            logger.info(f"Sandbox Turn {turn} Output:\n{output}")
            
            # Simple check for pytest success
            if "FAILED" not in output and "ERROR" not in output and ("PASSED" in output or "passed" in output):
                logger.info(f"Sandbox verification successfully passed on turn {turn}.")
                return env_id
                
            if turn == max_turns:
                logger.error("Sandbox reached maximum self-repair turns without resolving errors.")
                raise ValueError(f"Antigravity sandbox failed to stabilize code after {max_turns} repair turns.")
                
            logger.warning(f"Test failed or returned error in sandbox. Initiating self-repair (Turn {turn}/{max_turns})...")
            
            # Request self-repair on the same environment referencing the correct previous interaction
            interaction = self.client.interactions.create(
                agent=self.model_agent,
                environment=env_id,
                previous_interaction_id=last_id,
                input="The test failed or was incomplete. Analyze the stderr/stdout, fix the files, and re-run pytest."
            )
            last_id = interaction.id
            turn += 1

        return env_id

    def download_skill_snapshot(self, env_id: str, output_dir: str):
        """
        Downloads the sandbox environment snapshot via HTTP GET and unpacks the verified code.
        """
        url = f"https://generativelanguage.googleapis.com/v1beta/files/environment-{env_id}:download"
        headers = {"x-goog-api-key": GEMINI_API_KEY} if GEMINI_API_KEY else {}
        
        temp_tar_path = os.path.join(output_dir, f"env_{env_id}.tar")
        os.makedirs(output_dir, exist_ok=True)
        
        logger.info(f"Downloading sandbox snapshot from {url}...")
        
        with httpx.Client() as client:
            response = client.get(url, headers=headers, timeout=60.0)
            if response.status_code != 200:
                logger.error(f"Failed to download sandbox snapshot: {response.status_code} - {response.text}")
                raise RuntimeError(f"Failed to download sandbox snapshot: {response.text}")
                
            with open(temp_tar_path, "wb") as f:
                f.write(response.content)
                
        logger.info(f"Snapshot downloaded successfully to {temp_tar_path}. Extracting 'src/' path...")
        
        # Unpack only files in the src/ directory into output_dir
        with tarfile.open(temp_tar_path, "r") as tar:
            for member in tar.getmembers():
                # Extract members starting with 'src/' or in the target src directory
                if member.name.startswith("src/"):
                    # We strip the leading 'src/' component to place it directly in output_dir
                    member.name = member.name.replace("src/", "", 1)
                    if member.name:  # Avoid root directory extract
                        tar.extract(member, path=output_dir)
                        logger.info(f"Extracted {member.name} to {output_dir}")
                        
        # Clean up local temporary tar file
        try:
            os.remove(temp_tar_path)
            logger.info("Temporary sandbox tarball cleaned up.")
        except Exception as e:
            logger.warning(f"Failed to delete temp file {temp_tar_path}: {e}")

    def download_validated_code_text(self, env_id: str, last_id: str, skill_name: str) -> str:
        """
        Fallback method to retrieve code as printed text if Snapshot Download fails.
        """
        logger.info("Requesting raw code output fallback from sandbox...")
        interaction_download = self.client.interactions.create(
            agent=self.model_agent,
            environment=env_id,
            previous_interaction_id=last_id,
            input=f"Print the final validated contents of `src/{skill_name}.py` inside triple-backticks (```python) so it can be parsed."
        )
        
        return self._parse_code_blocks(interaction_download.output_text)

    def _parse_code_blocks(self, text: str) -> str:
        blocks = re.findall(r"```python\n(.*?)\n```", text, re.DOTALL)
        if blocks:
            return blocks[0]
        # Return fallback text if markdown block missing
        return text
