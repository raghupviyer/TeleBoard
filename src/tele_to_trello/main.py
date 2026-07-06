#!/usr/bin/env python
import os
import json
import sys
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from dotenv import load_dotenv

import nest_asyncio
nest_asyncio.apply()

from crewai.flow.flow import Flow, listen, start
from tele_to_trello.crews.telegram_jira_crew import TelegramJiraCrew
from tele_to_trello.utils.mcp_client import JiraMCPClient

# Load env variables
load_dotenv()

OUTPUT_REPORT_FILE = Path("output_recommendations.md")

class TelegramJiraState(BaseModel):
    messages_to_process: List[Dict[str, Any]] = Field(default_factory=list)
    processed_count: int = 0
    results: List[Dict[str, Any]] = Field(default_factory=list)

class TelegramJiraFlow(Flow[TelegramJiraState]):

    @start()
    def fetch_messages(self):
        """Fetch incoming Telegram messages from the live API."""
        print("=== Step 1: Fetching Messages ===")
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        if not bot_token or not chat_id:
            raise ValueError(
                "\n========================================================================\n"
                "CONFIGURATION ERROR: Telegram Bot Credentials are missing in your .env.\n"
                "Please configure the following keys to receive messages:\n"
                "  - TELEGRAM_BOT_TOKEN (your telegram bot token from BotFather)\n"
                "  - TELEGRAM_CHAT_ID (the group chat ID to monitor)\n"
                "========================================================================\n"
            )

        # Read last processed update_id from offset file
        offset_file = Path("last_update_id.txt")
        offset = None
        if offset_file.exists():
            try:
                with open(offset_file, "r") as f:
                    offset = int(f.read().strip()) + 1
            except Exception as e:
                print(f"Error reading offset file: {e}")

        print(f"Fetching messages from live Telegram API (polling chat {chat_id}, offset={offset})...")
        try:
            messages = self._fetch_live_telegram_messages(bot_token, chat_id, offset)
            self.state.messages_to_process = messages
            print(f"Fetched {len(messages)} new live messages.")
        except Exception as e:
            print(f"\nERROR: Failed to connect to live Telegram API: {e}\n", file=sys.stderr)
            raise e

    @listen(fetch_messages)
    def process_messages(self):
        """Process each message through the CrewAI agents."""
        if not self.state.messages_to_process:
            print("No new messages to process.")
            return

        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        approver_ids_str = os.getenv("TELEGRAM_APPROVER_IDS", "")
        approver_ids = [aid.strip() for aid in approver_ids_str.split(",") if aid.strip()]

        print(f"\n=== Step 2: Processing {len(self.state.messages_to_process)} Messages ===")
        
        # Load knowledge base files statically for token-optimized context injection
        current = Path(__file__).resolve().parent
        project_root = None
        for parent in [current] + list(current.parents):
            if (parent / "pyproject.toml").exists() or (parent / "knowledge").exists():
                project_root = parent
                break
        if not project_root:
            project_root = Path.cwd()

        company_policies_path = project_root / "knowledge" / "company_policies.txt"
        product_specs_path = project_root / "knowledge" / "product_specs.txt"

        company_policies = ""
        if company_policies_path.exists():
            with open(company_policies_path, "r") as f:
                company_policies = f.read()

        product_specs = ""
        if product_specs_path.exists():
            with open(product_specs_path, "r") as f:
                product_specs = f.read()

        # Pre-fetch open issues to inject into the LLM context directly
        print("Fetching open tickets from Jira...")
        jira_client = JiraMCPClient()
        raw_issues = []
        try:
            raw_issues = jira_client.search_issues("")
            open_issues_list = []
            for issue in raw_issues:
                fields = issue.get("fields", {})
                status = fields.get("status", {}).get("name", "To Do")
                if status.lower() not in ["done", "resolved", "completed"]:
                    open_issues_list.append(f"- [{issue['key']}] {fields.get('summary')} (Status: {status})")
            jira_tickets_str = "\n".join(open_issues_list) if open_issues_list else "No open tickets."
        except Exception as e:
            print(f"Warning: Failed to fetch open issues from Jira: {e}")
            jira_tickets_str = "No open tickets (failed to connect)."

        for msg in self.state.messages_to_process:
            print(f"\n--- Processing Message ID {msg['id']} from {msg['sender']} ---")
            
            reply_to = msg.get("reply_to")
            reply_context = ""
            if reply_to:
                reply_context = (
                    f"\nReply Context:\n"
                    f"- This message is a direct reply to a previous message from {reply_to.get('sender')} "
                    f"sent at {reply_to.get('timestamp')}:\n"
                    f"  \"{reply_to.get('text')}\""
                )
            
            inputs = {
                "sender": msg["sender"],
                "timestamp": msg["timestamp"],
                "text": msg["text"],
                "reply_to_message_context": reply_context,
                "jira_tickets": jira_tickets_str,
                "company_policies": company_policies,
                "product_specs": product_specs
            }

            primary_name = "Ollama" if os.getenv("OLLAMA_MODEL") else "Gemini"
            print(f"Executing single-stage coordinate_sync_task with primary {primary_name}...")
            try:
                # Try primary LLM first
                TelegramJiraCrew.use_nvidia = False
                crew_obj = TelegramJiraCrew()
                crew_instance = crew_obj.crew()
                crew_result = crew_instance.kickoff(inputs=inputs)
            except Exception as e:
                # Failover to NVIDIA NIM Llama 3.1 70B
                print(f"Primary {primary_name} execution failed or rate-limited ({e}). Falling back to NVIDIA NIM...")
                TelegramJiraCrew.use_nvidia = True
                crew_obj = TelegramJiraCrew()
                crew_instance = crew_obj.crew()
                crew_result = crew_instance.kickoff(inputs=inputs)
            
            # Parse response
            resolution = crew_result.pydantic
            
            category = "uncategorized"
            action_required = "prompt_user"
            matching_jira_key = None
            recommended_response = ""
            followup_draft = ""
            rationale = ""
            target_jira_status = None
            
            if resolution:
                category = getattr(resolution, "category", "uncategorized").strip().lower()
                action_required = getattr(resolution, "action_required", "prompt_user").strip().lower()
                matching_jira_key = getattr(resolution, "matching_jira_key", None)
                recommended_response = getattr(resolution, "recommended_response", "")
                followup_draft = getattr(resolution, "followup_draft", "")
                rationale = getattr(resolution, "rationale", "")
                target_jira_status = getattr(resolution, "target_jira_status", None)
            else:
                print("Warning: Pydantic parsing failed. Falling back to default values.")
                
            print(f"Coordinator determined category: '{category}', Action required: '{action_required}', Match key: '{matching_jira_key}', Target Status: '{target_jira_status}'")

            outcome = ""
            # Execute actions based on LLM decision
            if action_required == "prompt_user" or category == "uncategorized":
                print("Interactive Prompt Flow triggered for message.")
                prompt_text = (
                    f"⚠️ **Uncategorized Message Received**\n"
                    f"**From:** {msg['sender']}\n"
                    f"**Message:** \"{msg['text']}\"\n\n"
                    f"Is this a junk message, or should it belong to an existing ticket?"
                )
                buttons = [
                    [
                        {"text": "🗑️ Mark as Junk", "callback_data": "choice_junk"},
                        {"text": "📁 Associate with Ticket", "callback_data": "choice_associate"}
                    ]
                ]
                
                try:
                    # Send prompt to all approvers (or fallback to chat_id group)
                    prompts = self._send_telegram_prompt_to_approvers(bot_token, approver_ids, chat_id, prompt_text, buttons)
                    if not prompts:
                        raise ValueError("No prompt messages could be sent.")
                        
                    res_dict = self._poll_telegram_selection_multi(bot_token, prompts, timeout_sec=None)
                    
                    if res_dict:
                        selection = res_dict["data"]
                        responder = res_dict["responder"]
                        winner_cid = res_dict["winner_chat_id"]
                        winner_mid = res_dict["winner_msg_id"]
                        
                        if selection == "choice_junk":
                            # Update winner chat
                            self._edit_telegram_message(bot_token, winner_cid, winner_mid, f"🗑️ Marked as Junk by {responder}. No action taken.")
                            # Update other chats
                            for cid, mid in prompts.items():
                                if cid != winner_cid:
                                    self._edit_telegram_message(bot_token, cid, mid, f"🗑️ This request was marked as Junk by {responder}.")
                            outcome = f"Marked as junk by {responder}. No Jira action taken."
                            
                        elif selection == "choice_associate":
                            # Update all other chats first to show that someone is handling it
                            for cid, mid in prompts.items():
                                if cid != winner_cid:
                                    self._edit_telegram_message(bot_token, cid, mid, f"⌛ This request is being associated with a ticket by {responder}...")
                                    
                            ticket_buttons = []
                            # Present open issues
                            for issue in raw_issues[:8]:
                                key = issue.get("key", "N/A")
                                summary = issue.get("fields", {}).get("summary", "No Summary")
                                if len(summary) > 25:
                                    summary = summary[:22] + "..."
                                ticket_buttons.append([{"text": f"📁 {key}: {summary}", "callback_data": f"ticket_{key}"}])
                            
                            ticket_buttons.append([{"text": "🆕 Create New Ticket", "callback_data": "ticket_create_new"}])
                            ticket_buttons.append([{"text": "❌ Cancel", "callback_data": "ticket_cancel"}])
                            
                            self._edit_telegram_message_with_buttons(
                                bot_token, winner_cid, winner_mid, 
                                f"Select which Jira ticket to associate this message with (Requested by {responder}):", 
                                ticket_buttons
                            )
                            
                            # Poll only for the winner's choice
                            ticket_prompts = {winner_cid: winner_mid}
                            ticket_res = self._poll_telegram_selection_multi(bot_token, ticket_prompts, timeout_sec=None)
                            
                            if ticket_res and ticket_res["data"].startswith("ticket_"):
                                ticket_selection = ticket_res["data"]
                                action = ticket_selection[7:]
                                
                                if action == "create_new":
                                    print("Creating a new ticket...")
                                    created = jira_client.create_issue(
                                        f"Telegram Request from {msg['sender']}", 
                                        f"Sender: {msg['sender']}\nTimestamp: {msg['timestamp']}\nMessage: {msg['text']}", 
                                        "Bug"
                                    )
                                    new_key = created.get("key", "Unknown")
                                    # Update winner
                                    self._edit_telegram_message(bot_token, winner_cid, winner_mid, f"🆕 Created new Bug ticket {new_key} in Jira.")
                                    # Update others
                                    for cid, mid in prompts.items():
                                        if cid != winner_cid:
                                            self._edit_telegram_message(bot_token, cid, mid, f"🆕 Created new Bug ticket {new_key} in Jira (Handled by {responder}).")
                                    outcome = f"Created new Bug ticket {new_key} (Handled by {responder})."
                                    
                                elif action == "cancel":
                                    # Update winner
                                    self._edit_telegram_message(bot_token, winner_cid, winner_mid, f"❌ Cancelled by {responder}. No action taken.")
                                    # Update others
                                    for cid, mid in prompts.items():
                                        if cid != winner_cid:
                                            self._edit_telegram_message(bot_token, cid, mid, f"❌ Association cancelled by {responder}.")
                                    outcome = f"Cancelled by {responder}."
                                    
                                else:
                                    print(f"Adding comment to existing ticket {action}...")
                                    jira_client.add_comment(
                                        action, 
                                        f"Telegram comment from {msg['sender']} at {msg['timestamp']}:\n{msg['text']}"
                                    )
                                    transitioned = False
                                    if target_jira_status:
                                        transitioned = jira_client.transition_issue(action, target_jira_status)
                                    
                                    status_info = f" and transitioned to '{target_jira_status}'" if transitioned else ""
                                    # Update winner
                                    self._edit_telegram_message(bot_token, winner_cid, winner_mid, f"✅ Associated message with existing ticket {action}{status_info}.")
                                    # Update others
                                    for cid, mid in prompts.items():
                                        if cid != winner_cid:
                                            self._edit_telegram_message(bot_token, cid, mid, f"✅ Associated with existing ticket {action}{status_info} by {responder}.")
                                    outcome = f"Associated with existing ticket {action}{status_info} (Handled by {responder})."
                            else:
                                # Timeout or cancel on ticket selection
                                self._edit_telegram_message(bot_token, winner_cid, winner_mid, "⌛ Selection timed out or cancelled. No action taken.")
                                for cid, mid in prompts.items():
                                    if cid != winner_cid:
                                        self._edit_telegram_message(bot_token, cid, mid, "⌛ Selection timed out or cancelled.")
                                outcome = "Timed out waiting for ticket selection."
                        else:
                            pass
                    else:
                        # Timeout on initial choice
                        for cid, mid in prompts.items():
                            self._edit_telegram_message(bot_token, cid, mid, "⌛ Selection timed out or cancelled. No action taken.")
                        outcome = "Timed out waiting for initial choice."
                except Exception as e:
                    print(f"Error in interactive prompt flow: {e}")
                    outcome = f"Error during interactive prompting: {e}"

            elif action_required == "create_bug":
                print("Creating a new Bug in Jira...")
                try:
                    created = jira_client.create_issue(
                        f"Telegram Bug from {msg['sender']}: {msg['text'][:50]}...", 
                        f"Sender: {msg['sender']}\nTimestamp: {msg['timestamp']}\nMessage: {msg['text']}\n\nRationale:\n{rationale}", 
                        "Bug"
                    )
                    new_key = created.get("key", "Unknown")
                    outcome = f"Automatically created Bug ticket {new_key} in Jira."
                except Exception as e:
                    print(f"Error creating bug: {e}")
                    outcome = f"Failed to create Bug ticket in Jira: {e}"

            elif action_required == "create_task":
                print("Creating a new Task in Jira...")
                try:
                    created = jira_client.create_issue(
                        f"Telegram Request from {msg['sender']}: {msg['text'][:50]}...", 
                        f"Sender: {msg['sender']}\nTimestamp: {msg['timestamp']}\nMessage: {msg['text']}\n\nRationale:\n{rationale}", 
                        "Task"
                    )
                    new_key = created.get("key", "Unknown")
                    outcome = f"Automatically created Task ticket {new_key} in Jira."
                except Exception as e:
                    print(f"Error creating task: {e}")
                    outcome = f"Failed to create Task ticket in Jira: {e}"

            elif action_required == "add_comment":
                target_key = matching_jira_key
                if target_key:
                    print(f"Adding comment to existing ticket {target_key}...")
                    try:
                        jira_client.add_comment(
                            target_key, 
                            f"Telegram comment from {msg['sender']} at {msg['timestamp']}:\n{msg['text']}"
                        )
                        transitioned = False
                        if target_jira_status:
                            transitioned = jira_client.transition_issue(target_key, target_jira_status)
                        status_info = f" and transitioned status to '{target_jira_status}'" if transitioned else ""
                        outcome = f"Automatically associated, commented on existing ticket {target_key}{status_info}."
                    except Exception as e:
                        print(f"Error commenting on ticket {target_key}: {e}")
                        outcome = f"Failed to add comment to existing ticket {target_key}: {e}"
                else:
                    print("Warning: Action was add_comment but no matching_jira_key was provided. Creating new Bug instead.")
                    try:
                        created = jira_client.create_issue(
                            f"Telegram Bug from {msg['sender']}: {msg['text'][:50]}...", 
                            f"Sender: {msg['sender']}\nTimestamp: {msg['timestamp']}\nMessage: {msg['text']}\n\nRationale:\n{rationale}", 
                            "Bug"
                        )
                        new_key = created.get("key", "Unknown")
                        outcome = f"Created new Bug ticket {new_key} in Jira (no existing key matched)."
                    except Exception as e:
                        outcome = f"Failed to create ticket: {e}"

            elif action_required == "ignore":
                print("Ignored as noise/junk by coordinator.")
                outcome = "Ignored as noise/junk by LLM. No action taken."

            self.state.results.append({
                "message_id": msg["id"],
                "sender": msg["sender"],
                "text": msg["text"],
                "timestamp": msg["timestamp"],
                "outcome": outcome,
                "category": category,
                "recommended_response": recommended_response,
                "followup_draft": followup_draft,
                "rationale": rationale,
                "target_jira_status": target_jira_status
            })
            self.state.processed_count += 1
            
        print(f"\nProcessed {self.state.processed_count} messages successfully.")

    def _send_telegram_prompt_to_approvers(self, token: str, approver_ids: list, fallback_chat_id: str, text: str, buttons: list) -> dict:
        import requests
        results = {}
        target_ids = approver_ids if approver_ids else [fallback_chat_id]
        
        for tid in target_ids:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": tid,
                "text": text,
                "reply_markup": {
                    "inline_keyboard": buttons
                }
            }
            try:
                response = requests.post(url, json=payload, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    msg_id = data.get("result", {}).get("message_id")
                    if msg_id:
                        results[tid] = msg_id
            except Exception as e:
                print(f"Error sending prompt to chat {tid}: {e}")
        return results

    def _poll_telegram_selection_multi(self, token: str, prompts: dict, timeout_sec: int = None) -> dict:
        import requests
        import time
        start_time = time.time()
        offset = None
        
        prompt_msg_ids = list(prompts.values())
        print(f"Polling getUpdates for response to messages {prompt_msg_ids}...")
        
        while True:
            if timeout_sec is not None and (time.time() - start_time > timeout_sec):
                print("Interactive prompt selection timed out.")
                return None
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {}
            if offset:
                params["offset"] = offset
            try:
                response = requests.get(url, params=params, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok"):
                        for update in data.get("result", []):
                            update_id = update["update_id"]
                            offset = update_id + 1
                            
                            callback = update.get("callback_query")
                            if callback:
                                msg_id = callback.get("message", {}).get("message_id")
                                if msg_id in prompt_msg_ids:
                                    matched_chat_id = None
                                    for cid, mid in prompts.items():
                                        if mid == msg_id:
                                            matched_chat_id = cid
                                            break
                                    
                                    # Answer callback query
                                    ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                                    requests.post(ans_url, json={"callback_query_id": callback["id"]}, timeout=5)
                                    
                                    responder_name = callback.get("from", {}).get("first_name", "Someone")
                                    username = callback.get("from", {}).get("username")
                                    if username:
                                        responder_name = f"{responder_name} (@{username})"
                                    
                                    return {
                                        "data": callback.get("data"),
                                        "responder": responder_name,
                                        "winner_chat_id": matched_chat_id,
                                        "winner_msg_id": msg_id
                                    }
            except Exception as e:
                print(f"Error polling Telegram updates: {e}")
            time.sleep(2)
        return None

    def _edit_telegram_message(self, token: str, chat_id: str, message_id: int, new_text: str):
        import requests
        url = f"https://api.telegram.org/bot{token}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": new_text
        }
        requests.post(url, json=payload, timeout=10)

    def _edit_telegram_message_with_buttons(self, token: str, chat_id: str, message_id: int, new_text: str, buttons: list):
        import requests
        url = f"https://api.telegram.org/bot{token}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": new_text,
            "reply_markup": {
                "inline_keyboard": buttons
            }
        }
        requests.post(url, json=payload, timeout=10)

    @listen(process_messages)
    def save_and_report(self):
        """Write a markdown recommendation report with recommendations."""
        print("\n=== Step 3: Saving State and Generating Report ===")
        
        if self.state.results:
            report_lines = [
                "# Telegram to Jira Sync & Recommendations Report",
                f"Total Processed: {self.state.processed_count}\n",
            ]
            for res in self.state.results:
                report_lines.append(f"## Message #{res['message_id']} ({res['sender']})")
                report_lines.append(f"**Original Text**: `{res['text']}`")
                report_lines.append(f"**Sent At**: {res['timestamp']}")
                report_lines.append(f"**Category**: `{res['category']}`")
                report_lines.append(f"**Target Jira Status**: `{res.get('target_jira_status')}`\n")
                
                report_lines.append("### Recommended Action Taken:")
                report_lines.append(res['outcome'] + "\n")
                
                report_lines.append("### Recommended Response Draft:")
                report_lines.append(res['recommended_response'] if res['recommended_response'] else "None required.")
                report_lines.append("\n")
                
                report_lines.append("### Proactive Follow-up Draft:")
                report_lines.append(res['followup_draft'] if res['followup_draft'] else "None required.")
                report_lines.append("\n")
                
                report_lines.append("### Rationale:")
                report_lines.append(res['rationale'])
                report_lines.append("\n" + "-"*40 + "\n")
                
            with open(OUTPUT_REPORT_FILE, "w") as f:
                f.write("\n".join(report_lines))
            print(f"Report saved to {OUTPUT_REPORT_FILE}")

        # Save maximum update ID to prevent re-processing
        if self.state.messages_to_process:
            max_update_id = max(msg["id"] for msg in self.state.messages_to_process)
            offset_file = Path("last_update_id.txt")
            try:
                with open(offset_file, "w") as f:
                    f.write(str(max_update_id))
                print(f"Saved offset {max_update_id} to last_update_id.txt")
            except Exception as e:
                print(f"Error saving offset to file: {e}")

    def _fetch_live_telegram_messages(self, token: str, chat_id: str, offset: int = None) -> List[Dict[str, Any]]:
        import requests
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        params = {}
        if offset:
            params["offset"] = offset
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        results = []
        if data.get("ok"):
            for update in data.get("result", []):
                message = update.get("message")
                if message and str(message.get("chat", {}).get("id")) == str(chat_id):
                    reply_to = message.get("reply_to_message")
                    reply_data = None
                    if reply_to:
                        reply_sender = f"{reply_to.get('from', {}).get('first_name', 'Unknown')} ({reply_to.get('from', {}).get('username', '')})"
                        reply_data = {
                            "sender": reply_sender,
                            "text": reply_to.get("text", ""),
                            "timestamp": str(reply_to.get("date", ""))
                        }
                    
                    results.append({
                        "id": update["update_id"],
                        "sender": f"{message.get('from', {}).get('first_name', 'Unknown')} ({message.get('from', {}).get('username', '')})",
                        "text": message.get("text", ""),
                        "timestamp": str(message.get("date")),
                        "processed": False,
                        "reply_to": reply_data
                    })
        return results

# --- AUTOMATIC JIRA FOLLOW-UP AGENT SCRIPT ---
def run_followup():
    """Reads pending Jira tickets, runs the knowledge responder to recommend automated follow-ups."""
    print("=== Checking Jira for Pending Tasks needing Follow-up ===")
    
    jira_client = JiraMCPClient()
    try:
        issues = jira_client.search_issues("")
    except Exception as e:
        print(f"\nERROR: Failed to fetch issues from Jira MCP: {e}\n", file=sys.stderr)
        raise e
        
    open_issues = []
    for issue in issues:
        fields = issue.get("fields", {})
        status = fields.get("status", {}).get("name", "To Do")
        if status.lower() not in ["done", "resolved", "completed"]:
            open_issues.append(issue)
            
    if not open_issues:
        print("No open Jira tickets found requiring follow-up.")
        return
        
    print(f"Found {len(open_issues)} open issues in Jira.")
    
    from crewai import Agent
    from tele_to_trello.crews.telegram_jira_crew import get_llm
    
    # Load knowledge files for context
    current = Path(__file__).resolve().parent
    project_root = None
    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").exists() or (parent / "knowledge").exists():
            project_root = parent
            break
    if not project_root:
        project_root = Path.cwd()

    company_policies_path = project_root / "knowledge" / "company_policies.txt"
    product_specs_path = project_root / "knowledge" / "product_specs.txt"

    company_policies = ""
    if company_policies_path.exists():
        with open(company_policies_path, "r") as f:
            company_policies = f.read()

    product_specs = ""
    if product_specs_path.exists():
        with open(product_specs_path, "r") as f:
            product_specs = f.read()
            
    followup_agent = Agent(
        role="Proactive Task Follow-up Specialist",
        goal="Analyze an open Jira issue, check its status and comment history, and generate a polite follow-up message to send to the provider to nudge progress based on company policy.",
        backstory="You are an active coordinator. You ensure that projects stay on track and providers meet their deadlines. You draft clear follow-up messages.",
        llm=get_llm(),
        verbose=True
    )
    
    report = ["# Proactive Jira Tasks Follow-up Report\n"]
    
    for issue in open_issues:
        key = issue.get("key")
        fields = issue.get("fields", {})
        summary = fields.get("summary")
        description = fields.get("description")
        status = fields.get("status", {}).get("name")
        comments = fields.get("comment", {}).get("comments", [])
        
        comment_history = "\n".join([f"- Comment: {c.get('body')}" for c in comments])
        
        prompt = f"""
        Jira Ticket Key: {key}
        Summary: {summary}
        Status: {status}
        Description: {description}
        Comment History:
        {comment_history}
        
        Company Operating & SLA Policies:
        {company_policies}
        
        Company Product Specs:
        {product_specs}
        
        Based on our company policy and SLA agreements, evaluate if this ticket needs a follow-up reminder to the provider. 
        If yes, write a draft follow-up reminder message we can post in the Telegram chat group. Include your rationale.
        """
        
        print(f"\n--- Checking Ticket {key}: {summary} ---")
        result = followup_agent.kickoff(prompt)
        
        report.append(f"## Issue: [{key}] - {summary}")
        report.append(f"**Current Status**: `{status}`")
        report.append("### Recommended Action & Follow-up Draft:")
        report.append(result.raw)
        report.append("\n" + "="*40 + "\n")
        
    followup_output = Path("output_followups.md")
    with open(followup_output, "w") as f:
        f.write("\n".join(report))
        
    print(f"\nFollow-up review complete. Report saved to {followup_output}")

def kickoff():
    """Main entrypoint that runs the flow once or continuously based on env configuration."""
    import time
    continuous = os.getenv("CONTINUOUS_MODE", "true").lower() == "true"
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))
    
    if not continuous:
        print("=== Starting Telegram-to-Jira Sync Agent (ONE-SHOT MODE) ===")
        flow = TelegramJiraFlow()
        flow.kickoff()
        return

    print("=== Starting Telegram-to-Jira Sync Agent (CONTINUOUS MODE) ===")
    print(f"Polling interval: {poll_interval} seconds. Press Ctrl+C to stop.\n")
    while True:
        try:
            flow = TelegramJiraFlow()
            flow.kickoff()
        except KeyboardInterrupt:
            print("\nStopping Agent...")
            break
        except Exception as e:
            print(f"\nError occurred during flow execution: {e}")
            print(f"Retrying in {poll_interval} seconds...")
            try:
                time.sleep(poll_interval)
            except KeyboardInterrupt:
                print("\nStopping Agent...")
                break
            continue
            
        print(f"\nWaiting {poll_interval} seconds for next check...")
        try:
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\nStopping Agent...")
            break

def run_with_trigger():
    """Simulate processing a single Telegram message passed in as a JSON arguments."""
    if len(sys.argv) < 2:
        print("Usage: uv run simulate_msg '<JSON_PAYLOAD>'")
        print("Example: uv run simulate_msg '{\"sender\": \"John (Client)\", \"text\": \"Checkout button is still failing\", \"timestamp\": \"2026-07-02T10:00:00Z\"}'")
        return
        
    try:
        payload = json.loads(sys.argv[1])
    except Exception as e:
        print(f"Error parsing JSON payload: {e}")
        return
        
    print(f"Simulating message handling with payload: {payload}")
    
    # Pre-fetch open issues to inject into the LLM context directly
    jira_client = JiraMCPClient()
    open_issues_list = []
    try:
        issues = jira_client.search_issues("")
        for issue in issues:
            fields = issue.get("fields", {})
            status = fields.get("status", {}).get("name", "To Do")
            if status.lower() not in ["done", "resolved", "completed"]:
                open_issues_list.append(f"- [{issue['key']}] {fields.get('summary')} (Status: {status})")
        jira_tickets_str = "\n".join(open_issues_list) if open_issues_list else "No open tickets."
    except Exception as e:
        jira_tickets_str = "No open tickets (failed to connect)."

    current = Path(__file__).resolve().parent
    project_root = None
    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").exists() or (parent / "knowledge").exists():
            project_root = parent
            break
    if not project_root:
        project_root = Path.cwd()

    company_policies_path = project_root / "knowledge" / "company_policies.txt"
    product_specs_path = project_root / "knowledge" / "product_specs.txt"

    company_policies = ""
    if company_policies_path.exists():
        with open(company_policies_path, "r") as f:
            company_policies = f.read()

    product_specs = ""
    if product_specs_path.exists():
        with open(product_specs_path, "r") as f:
            product_specs = f.read()

    reply_to = payload.get("reply_to")
    reply_context = ""
    if reply_to:
        reply_context = (
            f"\nReply Context:\n"
            f"- This message is a direct reply to a previous message from {reply_to.get('sender')} "
            f"sent at {reply_to.get('timestamp')}:\n"
            f"  \"{reply_to.get('text')}\""
        )

    inputs = {
        "sender": payload.get("sender", "Client"),
        "timestamp": payload.get("timestamp", "2026-07-02T12:00:00Z"),
        "text": payload.get("text", ""),
        "reply_to_message_context": reply_context,
        "jira_tickets": jira_tickets_str,
        "company_policies": company_policies,
        "product_specs": product_specs
    }

    primary_name = "Ollama" if os.getenv("OLLAMA_MODEL") else "Gemini"
    print(f"Executing single-stage coordinate_sync_task in simulation with primary {primary_name}...")
    try:
        # Try primary LLM first
        TelegramJiraCrew.use_nvidia = False
        crew_obj = TelegramJiraCrew()
        crew_instance = crew_obj.crew()
        result = crew_instance.kickoff(inputs=inputs)
    except Exception as e:
        # Failover to NVIDIA NIM Llama 3.1 70B
        print(f"Primary {primary_name} execution failed or rate-limited ({e}). Falling back to NVIDIA NIM...")
        TelegramJiraCrew.use_nvidia = True
        crew_obj = TelegramJiraCrew()
        crew_instance = crew_obj.crew()
        result = crew_instance.kickoff(inputs=inputs)

    print("\n=== Agent Results ===")
    print(result.raw)

if __name__ == "__main__":
    kickoff()
