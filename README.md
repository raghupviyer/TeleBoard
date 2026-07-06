# Telegram to Jira Multi-Agent CrewAI System

A multi-agent flow system built in Python using **CrewAI** that monitors a Telegram group conversation between a client (us) and a provider, automatically classifies requests, syncs conversations task-by-task to **Jira** using a Model Context Protocol (MCP) server, and recommends grounded replies and proactive follow-ups based on the company's knowledge base.

---

## System Architecture

The project implements a sequential flow orchestrating three core agents:

```
[ Telegram Chat ] ──► (TelegramJiraFlow) 
                           │
                           ├──► [Agent 1] Classifier (Categorizes: bug, feature, followup, query)
                           │
                           ├──► [Agent 2] Jira Manager (MCP search, create, or add comments)
                           │
                           └──► [Agent 3] Knowledge Responder (Grounded replies & nudges using OKF files)
```

1. **Telegram Classifier Agent**: Analyzes message text and categorizes it into a bug fix request, feature development request, task followup, or provider query.
2. **Jira Manager Agent**: Searches for existing issues using MCP tools. It creates new tickets for new requests, or appends conversation history as issue comments.
3. **Knowledge Responder Agent**: Grounded in company policy and technical specifications, this agent drafts recommended response replies.
4. **Proactive Follow-up Specialist**: A utility script/agent that audits open Jira issues, compares status/comments against SLA policies, and drafts follow-up nudges.

---

## Directory Structure

```
tele_to_trello/
├── pyproject.toml              # Dependencies (crewai, mcp, nest-asyncio, telethon)
├── .env                        # Local active configuration (keys, endpoints)
├── .env.example                # Configuration template
├── .gitignore                  # Git untracked settings
├── knowledge/                  # Company Knowledge Base directory
│   ├── company_policies.txt    # SLAs and communication policies
│   └── product_specs.txt       # Technical specifications (OKF markdown supported)
├── src/
│   └── tele_to_trello/
│       ├── __init__.py
│       ├── main.py             # Ingestion flow & follow-up runner
│       ├── crews/
│       │   ├── __init__.py
│       │   ├── telegram_jira_crew.py # Orchestrates agents & tasks
│       │   └── config/
│       │       ├── agents.yaml # Prompt configurations for agents
│       │       └── tasks.yaml  # Prompt configurations for tasks
│       ├── tools/
│       │   ├── __init__.py
│       │   └── jira_mcp_tool.py # CrewAI tool wrappers for the MCP Client
│       └── utils/
│           ├── __init__.py
│           └── mcp_client.py   # Resilient schema-aware MCP client session
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
    *   `OLLAMA_MODEL` & `OLLAMA_BASE_URL` (Optional): Specify these to connect the system to a local Ollama instance (e.g. `OLLAMA_MODEL=gemma4:latest` and `OLLAMA_BASE_URL=http://127.0.0.1:11434`). If set, Ollama is treated as the primary model.
    *   `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`: Credentials of the bot monitoring the group chat.
    *   `JIRA_HOST`, `JIRA_EMAIL`, `JIRA_API_TOKEN` & `JIRA_PROJECT_KEY`: Credentials for authentication with your Jira cloud instance.
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
uv run simulate_msg '{"sender": "John (Client)", "text": "Stripe checkout is throwing a 500 error", "timestamp": "2026-07-02T10:00:00Z"}'
```

---

## Inputs & Outputs Reference

### Inputs
*   **Telegram Messages**: Live JSON message formats polled from the chat group.
*   **Knowledge Base**: Documents inside the `knowledge/` folder. Supports `.txt` files and standard **Google Open Knowledge Format (OKF)** markdown files (`.md` files with YAML frontmatter) containing policies, SLAs, or technical specs.
*   **Jira Tickets**: Active ticket details and comment history retrieved dynamically by the Jira Manager.

### Outputs
*   **Jira Actions**: Creates new issues (e.g., `BUG-12`) or adds new comments containing the sender, timestamp, and context of conversations.
*   **Local Reports**:
    *   `output_recommendations.md`: Generated by the ingestion flow. Contains category classifications, issue mappings, and drafts for recommended responses.
    *   `output_followups.md`: Generated by the follow-up checker. Contains action items, rationales, and drafted nudges.
