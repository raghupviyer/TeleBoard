# Telegram to Jira Multi-Agent CrewAI System

A multi-agent flow system built in Python using **CrewAI** that monitors a Telegram group conversation, automatically classifies requests, syncs conversations task-by-task to **Jira** using a Model Context Protocol (MCP) server, and recommends grounded replies and proactive follow-ups based on the company's knowledge base.

---

## System Architecture

The project orchestrates two main workflows:

### 1. Ingestion Flow (Continuous Polling)
Monitors incoming messages and uses a unified coordinator agent to sync details to Jira:

```
[ Telegram Chat ] ──► (TelegramJiraFlow) 
                           │
                           └──► [Agent] Telegram & Jira Operations Sync Coordinator
                                     │
                                     ├──► 1. Affiliation Lookup (Client vs. Provider)
                                     ├──► 2. Category Classification (bug, feat, followup, query, junk)
                                     ├──► 3. Jira action mapping (search/link/comment/create)
                                     ├──► 4. Dynamic transition (To Do, In Progress, In Review)
                                     └──► 5. Recommended reply draft
```

*   **Coordinator Agent**: A unified `jira_coordinator` agent that acts in a single cognitive step to analyze context, determine affiliations, find matching Jira tickets, write comments, draft professional responses, and transition issue statuses. Doing this in a single step dramatically reduces token overhead and improves processing speed.
*   **Interactive Multi-Approver DM Flow**: When a message is classified as `uncategorized`, the bot sends private approval prompts to all configured administrator accounts (`TELEGRAM_APPROVER_IDS`). The first administrator to select an action claims the request; other administrators' DMs are immediately updated in real-time to prevent duplicate task mapping.

### 2. Proactive Follow-up Check
An automated script that audits open Jira issues, compares comment history against SLA policies, and drafts follow-up nudges:

```
[ Jira Open Tickets ] ──► (run_followup) ──► [Agent] Task Follow-up Specialist ──► output_followups.md
```

*   **Follow-up Agent**: Evaluates open tickets to check if they are stuck or missing updates, drafting polite progress reminders grounded in SLA policies.

---

## Directory Structure

```
tele_to_trello/
├── pyproject.toml              # Python dependencies (CrewAI, LiteLLM, Telethon)
├── .env                        # Local active configuration (keys, endpoints) - Git ignored
├── .env.example                # Configuration template for deployment
├── .gitignore                  # Git untracked settings
├── knowledge/                  # Company Knowledge Base directory
│   ├── company_policies.txt    # SLA limits, response times, and team affiliation definitions
│   └── product_specs.txt       # Technical specifications and API module definitions
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
│       │   └── jira_mcp_tool.py # CrewAI tool wrappers for the MCP Client
│       └── utils/
│           ├── __init__.py
│           └── mcp_client.py   # Resilient schema-aware Jira API & Agile Sprint MCP client
└── output_recommendations.md   # Report generated after running main flow
```

---

## Setup Instructions

### Prerequisites
*   **Python**: Version `>=3.10` and `<3.14`.
*   **uv**: Fast Python package manager (run `curl -LsSf https://astral.sh/uv/install.sh | sh` to install if needed).
*   **Node.js & npx**: Required to launch the Node-based Jira MCP server.

### Installation
1.  Navigate to the project directory:
    ```bash
    cd tele_to_trello
    ```
2.  Install dependencies and establish the virtual environment:
    ```bash
    uv sync
    ```

### Configuration
1.  Create your active configuration file:
    ```bash
    cp .env.example .env
    ```
2.  Open `.env` and fill in the required keys:
    *   `OPENAI_API_KEY` (or `GEMINI_API_KEY`): Used by the CrewAI framework for agent execution.
    *   `OLLAMA_MODEL` & `OLLAMA_BASE_URL` (Optional): Specify these to connect the system to a local Ollama instance (e.g. `OLLAMA_MODEL=gemma4:latest` and `OLLAMA_BASE_URL=http://127.0.0.1:11434`). If set, Ollama is treated as the primary model. The system automatically configures a context window length of `8192` (`num_ctx=8192`) for Ollama to prevent prompt truncation.
    *   `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`: Credentials of the bot monitoring the group chat.
    *   `TELEGRAM_APPROVER_IDS`: Comma-separated list of individual chat/user IDs authorized to receive private DM approvals (e.g., `123456789,987654321`). Falls back to the group chat if left blank.
    *   `JIRA_HOST`, `JIRA_EMAIL`, `JIRA_API_TOKEN` & `JIRA_PROJECT_KEY`: Credentials for authentication with your Jira cloud instance.
    *   `JIRA_DEFAULT_SPRINT_ID`: The active Sprint ID to assign newly created tickets automatically (e.g., `2` for Sprint 0). Leaves issues in the backlog if empty.
    *   `JIRA_MCP_COMMAND` & `JIRA_MCP_ARGS`: Startup command configuration to run the Node Jira MCP server subprocess (defaults to `npx` and `-y,@modelcontextprotocol/server-jira`).

---

## Operating Instructions

The system provides three command entrypoints via `uv run`:

### 1. Ingestion Flow (Continuous Polling)
Reads new messages from the Telegram chat, processes them through the CrewAI agents, syncs task details to Jira, and outputs recommended responses.
```bash
uv run run_flow
```

### 2. Proactive Follow-up Check
Checks all active/open Jira tickets, reviews status and comment history, and uses your company knowledge base to generate polite nudge follow-up messages for outstanding items.
```bash
uv run run_followup
```

### 3. Simulate a Single Message (Trigger Test)
Sends an ad-hoc message payload into the system to verify the pipeline manually.
```bash
uv run simulate_msg '{"sender": "@client_manager_user", "text": "Stripe checkout is throwing a 500 error", "timestamp": "2026-07-02T10:00:00Z"}'
```

---

## Inputs & Outputs Reference

### Inputs
*   **Telegram Messages**: Live JSON message formats polled from the chat group.
*   **Knowledge Base**: Documents inside the `knowledge/` folder. Supports `.txt` files containing policies, SLAs, or technical specs.
*   **Jira Tickets**: Active ticket details and comment history retrieved dynamically by the Jira Manager.

### Outputs
*   **Jira Actions**: Creates new issues (e.g., `SCRUM-10`), adds comments containing the sender and context of conversations, and transitions issue statuses (To Do, In Progress, In Review).
*   **Local Reports**:
    *   `output_recommendations.md`: Generated by the ingestion flow. Contains category classifications, target statuses, issue mappings, and drafts for recommended responses.
    *   `output_followups.md`: Generated by the follow-up checker. Contains action items, rationales, and drafted nudges.
