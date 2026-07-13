# Telegram to OpenProject Multi-Agent CrewAI System

A multi-agent flow system built in Python using **CrewAI** that monitors a Telegram group conversation, automatically classifies requests, syncs conversations task-by-task to **OpenProject** using its REST API v3, and recommends grounded replies and proactive follow-ups based on the company's knowledge base.

---

## System Architecture

The project orchestrates two main workflows:

### Ingestion Flow (Continuous Polling)

Monitors incoming messages and uses a unified coordinator agent to sync details to OpenProject:

```text
[ Telegram Chat ] ──► (TelegramOpenProjectFlow) 
                           │
                           └──► [Agent] Telegram & OpenProject Operations Sync Coordinator
                                     │
                                     ├──► 1. Affiliation Lookup (Client vs. Provider)
                                     ├──► 2. Category Classification (bug, feat, followup, query, junk)
                                     ├──► 3. OpenProject action mapping (search/link/comment/create)
                                     ├──► 4. Dynamic transition (To Do, In Progress, In Review)
                                     └──► 5. Recommended reply draft
```

* **Coordinator Agent**: A unified `openproject_coordinator` agent that acts in a single cognitive step to analyze context, determine affiliations, find matching OpenProject work packages, write comments, draft professional responses, and transition issue statuses. Doing this in a single step dramatically reduces token overhead and improves processing speed.
* **Interactive Multi-Approver DM Flow**: When a message is classified as `uncategorized`, the bot sends private approval prompts to all configured administrator accounts (`TELEGRAM_APPROVER_IDS`). The first administrator to select an action claims the request; other administrators' DMs are immediately updated in real-time to prevent duplicate task mapping.

### Proactive Follow-up Check

An automated script that audits open OpenProject work packages, compares comment history against SLA policies, and drafts follow-up nudges:

```text
[ OpenProject Open Work Packages ] ──► (run_followup) ──► [Agent] Task Follow-up Specialist ──► output_followups.md
```

* **Follow-up Agent**: Evaluates open tickets/work packages to check if they are stuck or missing updates, drafting polite progress reminders grounded in SLA policies.

---

## Directory Structure

```text
tele_to_trello/
├── pyproject.toml              # Python dependencies (CrewAI, LiteLLM, Telethon)
├── .env                        # Local active configuration (keys, endpoints) - Git ignored
├── .env.example                # Configuration template for deployment
├── .gitignore                  # Git untracked settings
├── knowledge/                  # Company Knowledge Base directory
├── src/
│   └── tele_to_trello/
│       ├── __init__.py
│       ├── main.py             # Ingestion flow & follow-up runner
│       ├── crews/
│       │   ├── __init__.py
│       │   ├── telegram_jira_crew.py # Crew configuration class & Pydantic SyncResolution schema
│       │   └── config/
│       │       ├── agents.yaml # Prompt configurations for agents
│       │       └── tasks.yaml  # Prompt configurations for tasks
│       ├── tools/
│       │   ├── __init__.py
│       │   └── jira_mcp_tool.py # CrewAI tool wrappers for the OpenProject Client
│       └── utils/
│           ├── __init__.py
│           └── mcp_client.py   # Resilient schema-aware OpenProject API v3 client
└── output_recommendations.md   # Report generated after running main flow
```

---

## Setup Instructions

### Prerequisites

* **Python**: Version `>=3.10` and `<3.14`.
* **uv**: Fast Python package manager (run `curl -LsSf https://astral.sh/uv/install.sh | sh` to install if needed).

### Installation

1. Navigate to the project directory:

   ```bash
   cd tele_to_trello
   ```

2. Install dependencies and establish the virtual environment:

   ```bash
   uv sync
   ```

### Configuration

1. Create your active configuration file:

   ```bash
   cp .env.example .env
   ```

2. Open `.env` and fill in the required keys:
   * `OPENAI_API_KEY` (or `GEMINI_API_KEY`): Used by the CrewAI framework for agent execution.
   * `OLLAMA_MODEL` & `OLLAMA_BASE_URL` (Optional): Specify these to connect the system to a local Ollama instance (e.g. `OLLAMA_MODEL=gemma4:latest` and `OLLAMA_BASE_URL=http://127.0.0.1:11434`). If set, Ollama is treated as the primary model. The system automatically configures a context window length of `8192` (`num_ctx=8192`) for Ollama to prevent prompt truncation.
   * `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`: Credentials of the bot monitoring the group chat.
   * `TELEGRAM_APPROVER_IDS`: Comma-separated list of individual chat/user IDs authorized to receive private DM approvals (e.g., `123456789,987654321`). Falls back to the group chat if left blank.
   * `OPENPROJECT_HOST`, `OPENPROJECT_API_KEY` & `OPENPROJECT_PROJECT_ID`: Credentials and project identifier for authentication with your OpenProject instance.

---

## Operating Instructions

The system provides three command entrypoints via `uv run`:

### Run Ingestion Flow

Reads new messages from the Telegram chat, processes them through the CrewAI agents, syncs task details to OpenProject, and outputs recommended responses.

```bash
uv run run_flow
```

### Run Follow-up Check

Checks all active/open OpenProject work packages, reviews status and comment/activity history, and uses your company knowledge base to generate polite nudge follow-up messages for outstanding items.

```bash
uv run run_followup
```

### Run Trigger Simulation

Sends an ad-hoc message payload into the system to verify the pipeline manually.

```bash
uv run simulate_msg '{"sender": "@client_manager_user", "text": "Stripe checkout is throwing a 500 error", "timestamp": "2026-07-02T10:00:00Z"}'
```

---

## Inputs & Outputs Reference

### Inputs

* **Telegram Messages**: Live JSON message formats polled from the chat group.
* **Knowledge Base**: Documents inside the `knowledge/` folder. Supports `.txt` files containing policies, SLAs, or technical specs.
* **OpenProject Work Packages**: Active work package details and activity history retrieved dynamically by the OpenProject client.

### Outputs

* **OpenProject Actions**: Creates new work packages (e.g., Bugs, Tasks), adds comments containing the sender and context of conversations, and transitions work package statuses (To Do, In Progress, In Review).
* **Local Reports**:
  * `output_recommendations.md`: Generated by the ingestion flow. Contains category classifications, target statuses, work package mappings, and drafts for recommended responses.
  * `output_followups.md`: Generated by the follow-up checker. Contains action items, rationales, and drafted nudges.
