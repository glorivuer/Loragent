import logging
from db.database import get_task
from agents.developer_agent import run as run_developer
from agents.finance_agent import run as run_finance
from agents.scheduler_agent import run as run_scheduler

logger = logging.getLogger(__name__)

async def run_workflow(task_id: str) -> str:
    """
    Core ADK Workflow router that retrieves a task, identifies its agent type,
    and runs the corresponding agent implementation.
    """
    logger.info(f"Routing task {task_id}...")
    task = get_task(task_id)
    if not task:
        raise ValueError(f"Task with ID {task_id} not found in database.")
        
    agent_type = task['agent_type']
    payload = task['payload']
    
    logger.info(f"Dispatching task {task_id} to agent: {agent_type}")
    
    if agent_type == 'developer':
        result = await run_developer(payload)
    elif agent_type == 'finance':
        result = await run_finance(payload)
    elif agent_type == 'scheduler':
        result = await run_scheduler(payload)
    else:
        raise ValueError(f"Unsupported agent type: {agent_type}")
        
    return result
