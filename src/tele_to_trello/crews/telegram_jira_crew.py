import os
from typing import List, Optional
from dotenv import load_dotenv
from crewai import Agent, Crew, Process, Task, LLM
from crewai.project import CrewBase, agent, crew, task
from crewai.agents.agent_builder.base_agent import BaseAgent
from pydantic import BaseModel, Field

# Load environment variables
load_dotenv()

def get_llm(use_nvidia=False) -> LLM:
    """Helper to select LLM based on environment variables with automatic fallbacks."""
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    openai_key = os.getenv("OPENAI_API_KEY", "")
    nvidia_key = os.getenv("NVIDIA_API_KEY", "") or os.getenv("NVDIA_API_KEY", "") or os.getenv("NVIDIA_NIM_ADMIN_KEY", "")
    ollama_model = os.getenv("OLLAMA_MODEL", "")
    ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    
    # Explicitly set keys in environment so LiteLLM fallback calls can find them
    if gemini_key:
        os.environ["GEMINI_API_KEY"] = gemini_key
    if nvidia_key:
        os.environ["NVIDIA_API_KEY"] = nvidia_key
        os.environ["NVIDIA_NIM_API_KEY"] = nvidia_key
        os.environ["NVIDIA_NIM_ADMIN_KEY"] = nvidia_key
    if ollama_base_url:
        os.environ["OLLAMA_BASE_URL"] = ollama_base_url
    
    # 1. Define all possible models we can use
    all_gemini_models = [
        "gemini/gemini-2.5-flash",
        "gemini/gemini-3.5-flash",
        "gemini/gemini-1.5-flash",
        "gemini/gemini-1.5-pro",
    ]
    
    all_nvidia_models = [
        "nvidia_nim/meta/llama-3.1-70b-instruct",
        "nvidia_nim/meta/llama-3.3-70b-instruct",
        "nvidia_nim/mistralai/mistral-large-2-instruct",
        "nvidia_nim/google/gemma-2-27b-it",
        "nvidia_nim/meta/llama-3.1-8b-instruct",
    ]
    
    # Prepend 'ollama/' prefix if not present
    ollama_formatted = ""
    if ollama_model:
        ollama_formatted = ollama_model if ollama_model.startswith("ollama/") else f"ollama/{ollama_model}"

    # 2. Build active list of models we actually have keys/configs for
    active_models = []
    
    if use_nvidia and nvidia_key:
        active_models.extend(all_nvidia_models)
        if gemini_key:
            active_models.extend(all_gemini_models)
        if ollama_formatted:
            active_models.append(ollama_formatted)
    else:
        if ollama_formatted:
            active_models.append(ollama_formatted)
        if gemini_key:
            active_models.extend(all_gemini_models)
        if nvidia_key:
            active_models.extend(all_nvidia_models)
        
    # Fallback defaults if no keys are found in env
    if not active_models:
        active_models = ["gemini/gemini-2.5-flash", "gemini/gemini-3.5-flash"]
        
    # 3. Primary model is the first active one
    primary_model = active_models[0]
    
    # 4. Fallbacks list is everything else in the active list (excluding primary model)
    fallbacks = [m for m in active_models if m != primary_model]
    
    # Extract API key and base URL for primary model if explicitly needed (LiteLLM reads env)
    primary_key = ""
    base_url = None
    if primary_model.startswith("gemini/"):
        primary_key = gemini_key
    elif primary_model.startswith("nvidia_nim/"):
        primary_key = nvidia_key
    elif primary_model.startswith("openai/"):
        primary_key = openai_key
    elif primary_model.startswith("ollama/"):
        base_url = ollama_base_url

    print(f"[LLM Config] Primary model: {primary_model}")
    if base_url:
        print(f"[LLM Config] Base URL: {base_url}")
    print(f"[LLM Config] Failover Chain: {fallbacks}")
    
    llm_kwargs = {
        "model": primary_model,
        "api_key": primary_key if primary_key else None,
        "base_url": base_url,
        "fallbacks": fallbacks,
        "is_litellm": True,
        "temperature": 0.2
    }
    if primary_model.startswith("ollama/"):
        llm_kwargs["num_ctx"] = 8192
        llm_kwargs["options"] = {"num_ctx": 8192}
        
    return LLM(**llm_kwargs)

class SyncResolution(BaseModel):
    category: str = Field(description="Exactly one of: 'bug fix request', 'feat development request', 'followup on the task', 'followup question from provider', or 'uncategorized'")
    action_required: str = Field(description="The action we should take: 'create_bug', 'create_task', 'add_comment', 'prompt_user', or 'ignore'")
    matching_jira_key: Optional[str] = Field(description="The key of the matching open Jira ticket (e.g. 'SCRUM-8') if a match was identified. Otherwise null.")
    recommended_response: str = Field(description="A professional, polite reply to the Telegram message grounded in SLA guidelines.")
    followup_draft: Optional[str] = Field(description="Draft of proactive follow-up reminder if escalation or follow-up is needed. Otherwise null.")
    rationale: str = Field(description="Brief explanation of which company policies or specifications were referenced and why.")
    target_jira_status: Optional[str] = Field(default=None, description="If a status update is appropriate for the matching ticket, specify the target status name: 'To Do', 'In Progress', 'In Review', or null.")

@CrewBase
class TelegramJiraCrew:
    """Telegram to Jira Sync & Recommendations Crew"""

    agents: List[BaseAgent]
    tasks: List[Task]

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    @agent
    def jira_coordinator(self) -> Agent:
        use_nvidia = getattr(self, "use_nvidia", False)
        return Agent(
            config=self.agents_config["jira_coordinator"],  # type: ignore[index]
            llm=get_llm(use_nvidia=use_nvidia),
            verbose=True,
        )

    @task
    def coordinate_sync_task(self) -> Task:
        return Task(
            config=self.tasks_config["coordinate_sync_task"],  # type: ignore[index]
            output_pydantic=SyncResolution
        )

    @crew
    def crew(self) -> Crew:
        """Creates the TelegramJiraCrew"""
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
        )
