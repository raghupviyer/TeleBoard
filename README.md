# Telegram to Ticketing System (Jira/OpenProject) Sync

A multi-agent system built in Python using **CrewAI** that monitors a Telegram group conversation, classifies requests, syncs conversations task-by-task to a ticketing system (**Jira** or **OpenProject**), and drafts follow-ups based on company policies.

---

## System Architecture

The project orchestrates two main workflows:

### Ingestion Flow (Continuous Polling)

Monitors incoming messages and uses a coordinator agent to sync details to the configured ticketing system:

```text
[ Telegram Chat ] ──► (TelegramSyncFlow) 
                           │
                           └──► [Agent] Sync Coordinator
                                     │
                                     ├──► 1. Affiliation Lookup (Client vs. Provider)
                                     ├──► 2. Category Classification (bug, feat, followup, query, junk)
                                     ├──► 3. Ticketing system action (search/link/comment/create)
                                     ├──► 4. Dynamic transition (To Do, In Progress, In Review)
                                     └──► 5. Recommended reply draft
```

* **Coordinator Agent**: The `sync_coordinator` agent analyzes context, determines affiliations, finds matching tickets, writes comments, drafts responses, and transitions issue statuses. 
* **Interactive Approver DM Flow**: When a message is classified as `uncategorized`, the bot sends private approval prompts to all configured administrator accounts (via `TELEGRAM_APPROVER_IDS`). Administrators can also dynamically assign new users to either the "Client" or "Provider" team and assign them approver roles through interactive Telegram buttons.

### Proactive Follow-up Check

An automated script that reviews open tickets, compares comment history against SLA policies, and drafts follow-up nudges:

```text
[ Open Tickets ] ──► (run_followup) ──► [Agent] Task Follow-up Specialist ──► output_followups.md
```

* **Follow-up Agent**: Evaluates open tickets to check if they are stuck or missing updates, drafting progress reminders based on SLA policies.

---

## Directory Structure

```text
tele_to_trello/
├── pyproject.toml              # Python dependencies
├── .env                        # Local configuration (Git ignored)
├── .env.example                # Configuration template
├── user_assignments.json       # Dynamic tracking of team/approver assignments
├── knowledge/                  # Company Knowledge Base directory
├── src/
│   └── tele_to_trello/
│       ├── main.py             # Ingestion flow & follow-up runner
│       ├── crews/
│       │   ├── telegram_jira_crew.py # Crew configuration class & schemas
│       │   └── config/
│       │       ├── agents.yaml # Prompt configurations for agents
│       │       └── tasks.yaml  # Prompt configurations for tasks
│       ├── tools/
│       │   └── jira_mcp_tool.py # Tools for ticket system interactions
│       └── utils/
│           └── mcp_client.py   # API clients for Jira and OpenProject
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
   * **LLM Selection**: Provide API keys for the model you want to use (`GEMINI_API_KEY`, `OPENAI_API_KEY`, or NVIDIA/Ollama config). The system defaults to Gemini, but will use Ollama or Nvidia NIM if configured or as a fallback.
   * **Telegram Settings**: 
     * `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`: Credentials of the bot monitoring the group chat.
     * `TELEGRAM_APPROVER_IDS`: Comma-separated list of individual chat/user IDs authorized to receive private DM approvals.
   * **Ticketing Provider**: Set `SYNC_PROVIDER` to either `jira` or `openproject`.
   * **Jira Configuration** (If using Jira): `JIRA_HOST`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`. Optionally `JIRA_DEFAULT_SPRINT_ID`.
   * **OpenProject Configuration** (If using OpenProject): `OPENPROJECT_HOST`, `OPENPROJECT_API_KEY`, `OPENPROJECT_PROJECT_ID`.

---

## Operating Instructions

The system provides three command entrypoints via `uv run`:

### Run Ingestion Flow

Reads new messages from the Telegram chat, processes them through the CrewAI agent, syncs task details to the ticketing system, and outputs recommended responses. By default, it runs continuously based on the `CONTINUOUS_MODE` environment variable.

```bash
uv run run_flow
```

### Run Follow-up Check

Checks all active/open tickets in your provider, reviews status and comment history, and uses your company knowledge base to generate follow-up messages for outstanding items.

```bash
uv run run_followup
```

### Run Trigger Simulation

Sends an ad-hoc message payload into the system to verify the pipeline manually.

```bash
uv run simulate_msg '{"sender": "John", "text": "Checkout is throwing an error", "timestamp": "2026-07-02T10:00:00Z"}'
```

---

## Inputs & Outputs Reference

### Inputs

* **Telegram Messages**: Live JSON message formats polled from the chat group.
* **Knowledge Base**: Documents inside the `knowledge/` folder containing policies and technical specs.
* **Ticketing System**: Active tickets and activity history retrieved dynamically by the API client.

### Outputs

* **Ticket Actions**: Creates new issues (e.g., Bugs, Tasks), adds comments, and transitions statuses.
* **Local Reports**:
  * `output_recommendations.md`: Generated by the ingestion flow.
  * `output_followups.md`: Generated by the follow-up checker.
