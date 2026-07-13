#!/usr/bin/env python
import os
import json
import sys
import html
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from dotenv import load_dotenv

import nest_asyncio
nest_asyncio.apply()

from crewai.flow.flow import Flow, listen, start
from tele_to_trello.crews.telegram_jira_crew import TelegramSyncCrew, SyncResolution
from tele_to_trello.utils.mcp_client import get_ticket_client

# Load env variables
load_dotenv(override=True)

OUTPUT_REPORT_FILE = Path("output_recommendations.md")

def load_all_user_assignments() -> dict:
    import json
    from pathlib import Path
    
    current = Path(__file__).resolve().parent
    project_root = None
    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").exists() or (parent / "knowledge").exists():
            project_root = parent
            break
    if not project_root:
        project_root = Path.cwd()
        
    path = project_root / "user_assignments.json"
    
    if not path.exists():
        return {}
        
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading user_assignments.json: {e}")
        return {}

def save_all_user_assignments(assignments: dict):
    import json
    from pathlib import Path
    
    current = Path(__file__).resolve().parent
    project_root = None
    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").exists() or (parent / "knowledge").exists():
            project_root = parent
            break
    if not project_root:
        project_root = Path.cwd()
        
    path = project_root / "user_assignments.json"
    
    try:
        with open(path, "w") as f:
            json.dump(assignments, f, indent=2)
    except Exception as e:
        print(f"Error saving user_assignments.json: {e}")

def load_user_assignments(chat_id: str) -> dict:
    all_assign = load_all_user_assignments()
    chat_key = str(chat_id)
    
    if chat_key not in all_assign:
        initial_approvers = {}
        approver_ids_str = os.getenv("TELEGRAM_APPROVER_IDS", "")
        approver_ids = [aid.strip() for aid in approver_ids_str.split(",") if aid.strip()]
        for aid in approver_ids:
            initial_approvers[str(aid)] = {
                "name": f"Admin (ID: {aid})",
                "team": "client",
                "is_approver": True
            }
        all_assign[chat_key] = initial_approvers
        save_all_user_assignments(all_assign)
        print(f"Initialized user_assignments.json for chat {chat_id} with {len(initial_approvers)} static approvers.")
        return initial_approvers
        
    return all_assign[chat_key]

def save_user_assignment(chat_id: str, user_id: str, name: str, team: str, is_approver: bool):
    all_assign = load_all_user_assignments()
    chat_key = str(chat_id)
    
    if chat_key not in all_assign:
        all_assign[chat_key] = {}
        
    all_assign[chat_key][str(user_id)] = {
        "name": name,
        "team": team,
        "is_approver": is_approver
    }
    
    save_all_user_assignments(all_assign)
    print(f"Saved assignment for user {user_id} ({name}) in chat {chat_id}: team={team}, is_approver={is_approver}")

class TelegramSyncState(BaseModel):
    messages_to_process: List[Dict[str, Any]] = Field(default_factory=list)
    processed_count: int = 0
    results: List[Dict[str, Any]] = Field(default_factory=list)

class TelegramSyncFlow(Flow[TelegramSyncState]):

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

        # Load dynamic user assignments
        assignments = load_user_assignments(chat_id)
        all_approvers = list(approver_ids)
        for uid, info in assignments.items():
            if info.get("is_approver") and uid not in all_approvers:
                all_approvers.append(uid)

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

        provider = os.getenv("SYNC_PROVIDER", "jira").strip().upper()
        print(f"Fetching open tickets from {provider}...")
        ticket_client = get_ticket_client()
        raw_issues = []
        try:
            raw_issues = ticket_client.search_issues("", include_comments=False)
            open_issues_list = []
            for issue in raw_issues:
                fields = issue.get("fields", {})
                status = fields.get("status", {}).get("name", "To Do")
                if status.lower() not in ["done", "resolved", "completed", "closed"]:
                    open_issues_list.append(f"- [{issue['key']}] {fields.get('summary')} (Status: {status})")
            open_tickets_str = "\n".join(open_issues_list) if open_issues_list else "No open tickets."
        except Exception as e:
            print(f"Warning: Failed to fetch open tickets from {provider}: {e}")
            open_tickets_str = "No open tickets (failed to connect)."

        seen_messages = set()
        for msg in self.state.messages_to_process:
            print(f"\n--- Processing Message ID {msg['id']} from {msg['sender']} ---")
            
            # Check for duplicate message text from the same sender in the current batch
            msg_text_clean = (msg.get("text") or "").strip()
            msg_key = (msg["sender"], msg_text_clean)
            if msg_text_clean and msg_key in seen_messages:
                print(f"Skipping duplicate message ID {msg['id']} in current batch.")
                self.state.results.append({
                    "message_id": msg["id"],
                    "sender": msg["sender"],
                    "text": msg["text"],
                    "timestamp": msg["timestamp"],
                    "outcome": "Ignored as duplicate message in the same batch.",
                    "category": "uncategorized",
                    "recommended_response": "",
                    "followup_draft": "",
                    "rationale": "Duplicate message in the same batch.",
                    "target_ticket_status": None
                })
                self.state.processed_count += 1
                continue
            if msg_text_clean:
                seen_messages.add(msg_key)

            try:
                sender_id = msg.get("sender_id")
                if sender_id and sender_id not in assignments and sender_id not in approver_ids:
                    print(f"New user detected: {msg['sender']} (ID: {sender_id}). Requesting team assignment...")
                    
                    # Prompt 1: Team Assignment
                    sender_escaped = html.escape(msg['sender'])
                    text_escaped = html.escape(msg['text'] if msg['text'] else "(No text content)")
                    prompt_text = (
                        f"👤 <b>New User Detected</b>\n"
                        f"<b>Name:</b> {sender_escaped}\n"
                        f"<b>User ID:</b> {sender_id}\n"
                        f"<b>Message:</b> \"{text_escaped}\"\n\n"
                        f"Please assign this user to a team:"
                    )
                    buttons = [
                        [
                            {"text": "👥 Client Side", "callback_data": f"team_client_{sender_id}"},
                            {"text": "🛠️ Provider Side", "callback_data": f"team_provider_{sender_id}"}
                        ]
                    ]
                    
                    try:
                        # Send prompt to all active approver IDs (or fallback to group chat if none)
                        prompts = self._send_telegram_prompt_to_approvers(bot_token, all_approvers, chat_id, prompt_text, buttons)
                        if not prompts:
                            raise ValueError("No prompt messages could be sent.")
                        
                        # Poll for selection
                        res_dict = self._poll_telegram_selection_multi(bot_token, prompts, timeout_sec=None)
                        
                        if not res_dict:
                            # Timeout
                            for cid, mid in prompts.items():
                                self._edit_telegram_message(bot_token, cid, mid, "⌛ Team assignment timed out. No action taken.")
                            raise TimeoutError("User team assignment timed out.")
                            
                        selection = res_dict["data"]
                        responder = res_dict["responder"]
                        winner_cid = res_dict["winner_chat_id"]
                        winner_mid = res_dict["winner_msg_id"]
                        
                        if selection.startswith("team_client_"):
                            # Update prompt text first to show assignment
                            self._edit_telegram_message(bot_token, winner_cid, winner_mid, f"👥 Assigned to Client Side by {responder}. Deciding role...")
                            for cid, mid in prompts.items():
                                if cid != winner_cid:
                                    self._edit_telegram_message(bot_token, cid, mid, f"👥 Assigned to Client Side by {responder}.")
                                    
                            # Stage 2: Role Assignment (only send to the winning approver's DM)
                            role_prompt_text = (
                                f"🔑 <b>Approver Role Assignment</b>\n"
                                f"Should <b>{sender_escaped}</b> (ID: {sender_id}) be set as an Approver?\n"
                                f"(Approvers receive interactive prompts to verify new users and map tickets)"
                            )
                            role_buttons = [
                                [
                                    {"text": "✅ Yes (Approver)", "callback_data": f"role_approver_{sender_id}"},
                                    {"text": "❌ No (Member)", "callback_data": f"role_member_{sender_id}"}
                                ]
                            ]
                            
                            role_prompts = self._send_telegram_prompt_to_approvers(bot_token, [winner_cid], chat_id, role_prompt_text, role_buttons)
                            role_res = self._poll_telegram_selection_multi(bot_token, role_prompts, timeout_sec=None)
                            
                            is_approver = False
                            role_desc = "Member"
                            if role_res:
                                role_selection = role_res["data"]
                                role_responder = role_res["responder"]
                                role_winner_cid = role_res["winner_chat_id"]
                                role_winner_mid = role_res["winner_msg_id"]
                                
                                if role_selection.startswith("role_approver_"):
                                    is_approver = True
                                    role_desc = "Approver"
                                    
                                self._edit_telegram_message(bot_token, role_winner_cid, role_winner_mid, f"✅ Set as Client {role_desc} by {role_responder}.")
                            else:
                                # Timeout on role selection
                                self._edit_telegram_message(bot_token, winner_cid, list(role_prompts.values())[0], f"⌛ Role assignment timed out. Defaulted to Member.")
                                
                            # Save assignment
                            save_user_assignment(chat_id, sender_id, msg["sender"], "client", is_approver)
                            # Reload assignments and update all_approvers
                            assignments = load_user_assignments(chat_id)
                            if is_approver and sender_id not in all_approvers:
                                all_approvers.append(sender_id)
                                
                        elif selection.startswith("team_provider_"):
                            # Update all messages
                            self._edit_telegram_message(bot_token, winner_cid, winner_mid, f"🛠️ Assigned to Provider Side by {responder}.")
                            for cid, mid in prompts.items():
                                if cid != winner_cid:
                                    self._edit_telegram_message(bot_token, cid, mid, f"🛠️ Assigned to Provider Side by {responder}.")
                                    
                            # Save assignment
                            save_user_assignment(chat_id, sender_id, msg["sender"], "provider", False)
                            # Reload assignments
                            assignments = load_user_assignments(chat_id)
                            
                    except TimeoutError as te:
                        print(f"User team assignment timed out: {te}")
                        outcome = "Timed out waiting for user team assignment."
                        self.state.results.append({
                            "message_id": msg["id"],
                            "sender": msg["sender"],
                            "text": msg["text"],
                            "timestamp": msg["timestamp"],
                            "outcome": outcome,
                            "category": "uncategorized",
                            "recommended_response": "",
                            "followup_draft": "",
                            "rationale": "Team assignment timed out.",
                            "target_ticket_status": None
                        })
                        self.state.processed_count += 1
                        continue
                    except Exception as e:
                        print(f"Error in dynamic user assignment flow: {e}")
                        outcome = f"Error during user team assignment: {e}"
                        self.state.results.append({
                            "message_id": msg["id"],
                            "sender": msg["sender"],
                            "text": msg["text"],
                            "timestamp": msg["timestamp"],
                            "outcome": outcome,
                            "category": "uncategorized",
                            "recommended_response": "",
                            "followup_draft": "",
                            "rationale": f"Error: {e}",
                            "target_ticket_status": None
                        })
                        self.state.processed_count += 1
                        continue

                # Build dynamic assignments context for this message processing
                current_assignments = load_user_assignments(chat_id)
                dynamic_assignments_str = ""
                client_names = []
                provider_names = []
                for uid, u in current_assignments.items():
                    if u.get("team") == "client":
                        client_names.append(f"{u.get('name')} (ID: {uid})")
                    elif u.get("team") == "provider":
                        provider_names.append(f"{u.get('name')} (ID: {uid})")
                        
                if client_names or provider_names:
                    dynamic_assignments_str = "\n\n### Dynamically Assigned Users (supplementing static lists):\n"
                    if client_names:
                        dynamic_assignments_str += f"- Client Side Senders: {', '.join(client_names)}\n"
                    if provider_names:
                        dynamic_assignments_str += f"- Provider Side Senders: {', '.join(provider_names)}\n"
                        
                msg_company_policies = company_policies + dynamic_assignments_str

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
                    "open_tickets": open_tickets_str,
                    "company_policies": msg_company_policies,
                    "product_specs": product_specs
                }

                primary_name = "Ollama" if os.getenv("OLLAMA_MODEL") else "Gemini"
                print(f"Executing single-stage coordinate_sync_task with primary {primary_name}...")
                try:
                    try:
                        # Try primary LLM first
                        TelegramSyncCrew.use_nvidia = False
                        crew_obj = TelegramSyncCrew()
                        crew_instance = crew_obj.crew()
                        crew_result = crew_instance.kickoff(inputs=inputs)
                    except Exception as e:
                        # Failover to NVIDIA NIM Llama 3.1 70B
                        print(f"Primary {primary_name} execution failed or rate-limited ({e}). Falling back to NVIDIA NIM...")
                        TelegramSyncCrew.use_nvidia = True
                        crew_obj = TelegramSyncCrew()
                        crew_instance = crew_obj.crew()
                        crew_result = crew_instance.kickoff(inputs=inputs)
                    
                    # Parse response
                    try:
                        import json
                        # Clean JSON output if model wrapped it in markdown code block
                        raw_content = crew_result.raw.strip()
                        if raw_content.startswith("```json"):
                            raw_content = raw_content[7:]
                        elif raw_content.startswith("```"):
                            raw_content = raw_content[3:]
                        if raw_content.endswith("```"):
                            raw_content = raw_content[:-3]
                        raw_content = raw_content.strip()
                        
                        data = json.loads(raw_content)
                        resolution = SyncResolution(**data)
                    except Exception as parse_err:
                        print(f"Failed to parse raw crew output as JSON: {parse_err}. Raw output was: {crew_result.raw}")
                        if crew_result.pydantic:
                            resolution = crew_result.pydantic
                        else:
                            raise parse_err
                except Exception as e:
                    print(f"Error: All LLM coordinator attempts failed for message {msg['id']}: {e}")
                    outcome = f"Failed to analyze message due to LLM error: {e}"
                    self.state.results.append({
                        "message_id": msg["id"],
                        "sender": msg["sender"],
                        "text": msg["text"],
                        "timestamp": msg["timestamp"],
                        "outcome": outcome,
                        "category": "uncategorized",
                        "recommended_response": "",
                        "followup_draft": "",
                        "rationale": f"LLM error: {e}",
                        "target_ticket_status": None
                    })
                    self.state.processed_count += 1
                    continue
                
                category = "uncategorized"
                action_required = "prompt_user"
                matching_ticket_key = None
                recommended_response = ""
                followup_draft = ""
                rationale = ""
                target_ticket_status = None
                
                if resolution:
                    category = getattr(resolution, "category", "uncategorized").strip().lower()
                    action_required = getattr(resolution, "action_required", "prompt_user").strip().lower()
                    matching_ticket_key = getattr(resolution, "matching_ticket_key", None)
                    recommended_response = getattr(resolution, "recommended_response", "")
                    followup_draft = getattr(resolution, "followup_draft", "")
                    rationale = getattr(resolution, "rationale", "")
                    target_ticket_status = getattr(resolution, "target_ticket_status", None)
                else:
                    print("Warning: Pydantic parsing failed. Falling back to default values.")
                    
                print(f"Coordinator determined category: '{category}', Action required: '{action_required}', Match ID/Key: '{matching_ticket_key}', Target Status: '{target_ticket_status}'")

                outcome = ""
                # Execute actions based on LLM decision
                if action_required == "prompt_user" or category == "uncategorized":
                    print("Interactive Prompt Flow triggered for message.")
                    msg_sender_escaped = html.escape(msg['sender'])
                    msg_text_escaped = html.escape(msg['text'] if msg['text'] else "(No text content)")
                    prompt_text = (
                        f"⚠️ <b>Uncategorized Message Received</b>\n"
                        f"<b>From:</b> {msg_sender_escaped}\n"
                        f"<b>Message:</b> \"{msg_text_escaped}\"\n\n"
                        f"Is this a junk message, or should it belong to an existing ticket in {provider}?"
                    )
                    buttons = [
                        [
                            {"text": "🗑️ Mark as Junk", "callback_data": "choice_junk"},
                            {"text": f"📁 Associate with {provider}", "callback_data": "choice_associate"}
                        ]
                    ]
                    
                    try:
                        # Send prompt to all approvers (or fallback to chat_id group)
                        prompts = self._send_telegram_prompt_to_approvers(bot_token, all_approvers, chat_id, prompt_text, buttons)
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
                                outcome = f"Marked as junk by {responder}. No {provider} action taken."
                                
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
                                    ticket_buttons.append([{"text": f"📁 Ticket {key}: {summary}", "callback_data": f"ticket_{key}"}])
                                
                                ticket_buttons.append([
                                    {"text": "🐛 Create New Bug", "callback_data": "ticket_create_bug"},
                                    {"text": "💡 Create Feature Request", "callback_data": "ticket_create_task"}
                                ])
                                ticket_buttons.append([{"text": "❌ Cancel", "callback_data": "ticket_cancel"}])
                                
                                self._edit_telegram_message_with_buttons(
                                        bot_token, winner_cid, winner_mid, 
                                        f"Select which {provider} ticket to associate this message with (Requested by {responder}):", 
                                        ticket_buttons
                                    )
                                
                                # Poll only for the winner's choice
                                ticket_prompts = {winner_cid: winner_mid}
                                ticket_res = self._poll_telegram_selection_multi(bot_token, ticket_prompts, timeout_sec=None)
                                
                                if ticket_res and ticket_res["data"].startswith("ticket_"):
                                    ticket_selection = ticket_res["data"]
                                    action = ticket_selection[7:]
                                    
                                    if action in ("create_bug", "create_task"):
                                        issue_type = "Bug" if action == "create_bug" else "Task"
                                        print(f"Creating a new {issue_type} in {provider}...")
                                        created = ticket_client.create_issue(
                                            f"Telegram Request from {msg['sender']}", 
                                            f"Sender: {msg['sender']}\nTimestamp: {msg['timestamp']}\nMessage: {msg['text']}", 
                                            issue_type
                                        )
                                        new_key = created.get("key", "Unknown")
                                        # Update winner
                                        self._edit_telegram_message(bot_token, winner_cid, winner_mid, f"🆕 Created new {issue_type} ticket {new_key} in {provider}.")
                                        # Update others
                                        for cid, mid in prompts.items():
                                            if cid != winner_cid:
                                                self._edit_telegram_message(bot_token, cid, mid, f"🆕 Created new {issue_type} ticket {new_key} in {provider} (Handled by {responder}).")
                                        outcome = f"Created new {issue_type} ticket {new_key} (Handled by {responder})."
                                        
                                    elif action == "cancel":
                                        # Update winner
                                        self._edit_telegram_message(bot_token, winner_cid, winner_mid, f"❌ Cancelled by {responder}. No action taken.")
                                        # Update others
                                        for cid, mid in prompts.items():
                                            if cid != winner_cid:
                                                self._edit_telegram_message(bot_token, cid, mid, f"❌ Association cancelled by {responder}.")
                                        outcome = f"Cancelled by {responder}."
                                        
                                    else:
                                        print(f"Adding comment to existing ticket {action} in {provider}...")
                                        ticket_client.add_comment(
                                            action, 
                                            f"Telegram comment from {msg['sender']} at {msg['timestamp']}:\n{msg['text']}"
                                        )
                                        transitioned = False
                                        if target_ticket_status:
                                            transitioned = ticket_client.transition_issue(action, target_ticket_status)
                                        
                                        status_info = f" and transitioned to '{target_ticket_status}'" if transitioned else ""
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
                    print(f"Creating a new Bug in {provider}...")
                    try:
                        created = ticket_client.create_issue(
                            f"Telegram Bug from {msg['sender']}: {msg['text']}", 
                            f"Sender: {msg['sender']}\nTimestamp: {msg['timestamp']}\nMessage: {msg['text']}\n\nRationale:\n{rationale}", 
                            "Bug"
                        )
                        new_key = created.get("key", "Unknown")
                        outcome = f"Automatically created Bug ticket {new_key} in {provider}."
                    except Exception as e:
                        print(f"Error creating bug: {e}")
                        outcome = f"Failed to create Bug ticket in {provider}: {e}"

                elif action_required == "create_task":
                    print(f"Creating a new Task in {provider}...")
                    try:
                        created = ticket_client.create_issue(
                            f"Telegram Request from {msg['sender']}: {msg['text']}", 
                            f"Sender: {msg['sender']}\nTimestamp: {msg['timestamp']}\nMessage: {msg['text']}\n\nRationale:\n{rationale}", 
                            "Task"
                        )
                        new_key = created.get("key", "Unknown")
                        outcome = f"Automatically created Task ticket {new_key} in {provider}."
                    except Exception as e:
                        print(f"Error creating task: {e}")
                        outcome = f"Failed to create Task ticket in {provider}: {e}"

                elif action_required == "add_comment":
                    target_key = matching_ticket_key
                    if target_key:
                        print(f"Adding comment to existing ticket {target_key} in {provider}...")
                        try:
                            ticket_client.add_comment(
                                target_key, 
                                f"Telegram comment from {msg['sender']} at {msg['timestamp']}:\n{msg['text']}"
                            )
                            transitioned = False
                            if target_ticket_status:
                                transitioned = ticket_client.transition_issue(target_key, target_ticket_status)
                            status_info = f" and transitioned status to '{target_ticket_status}'" if transitioned else ""
                            outcome = f"Automatically associated, commented on existing ticket {target_key}{status_info}."
                        except Exception as e:
                            print(f"Error commenting on ticket {target_key}: {e}")
                            outcome = f"Failed to add comment to existing ticket {target_key}: {e}"
                    else:
                        print(f"Warning: Action was add_comment but no matching_ticket_key was provided. Creating new Bug in {provider} instead.")
                        try:
                            created = ticket_client.create_issue(
                                f"Telegram Bug from {msg['sender']}: {msg['text']}", 
                                f"Sender: {msg['sender']}\nTimestamp: {msg['timestamp']}\nMessage: {msg['text']}\n\nRationale:\n{rationale}", 
                                "Bug"
                            )
                            new_key = created.get("key", "Unknown")
                            outcome = f"Created new Bug ticket {new_key} in {provider} (no existing key matched)."
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
                    "target_ticket_status": target_ticket_status
                })
                self.state.processed_count += 1

            except Exception as loop_err:
                print(f"CRITICAL SYSTEM ERROR: Failed to process message {msg['id']}: {loop_err}")
                self.state.results.append({
                    "message_id": msg["id"],
                    "sender": msg["sender"],
                    "text": msg["text"],
                    "timestamp": msg["timestamp"],
                    "outcome": f"Failed to process message due to system error: {loop_err}",
                    "category": "uncategorized",
                    "recommended_response": "",
                    "followup_draft": "",
                    "rationale": f"System crash: {loop_err}",
                    "target_ticket_status": None
                })
                self.state.processed_count += 1
            
        print(f"Processed {self.state.processed_count} messages successfully.")

    def _send_telegram_prompt_to_approvers(self, token: str, approver_ids: list, fallback_chat_id: str, text: str, buttons: list) -> dict:
        import requests
        results = {}
        # Strictly send to approver DMs. Never fall back to the group chat.
        target_ids = [str(aid) for aid in approver_ids if aid]
        if not target_ids:
            print("WARNING: No approver IDs configured in TELEGRAM_APPROVER_IDS or user_assignments.json. Cannot send DM prompt.")
            return {}
        
        env_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        for tid in target_ids:
            # Safety check: Never send any prompt or message to the group chat_id
            if (fallback_chat_id and str(tid) == str(fallback_chat_id)) or (env_chat_id and str(tid) == str(env_chat_id)):
                print(f"WARNING: Prevented sending prompt/message to the group chat {tid}")
                continue

            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": tid,
                "text": text,
                "parse_mode": "HTML",
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
                else:
                    print(f"Error: Failed to send prompt to chat {tid}. Telegram returned {response.status_code}: {response.text}")
                    if "bot can't initiate conversation" in response.text:
                        print(f"NOTE: The user with ID {tid} MUST search for your bot username in Telegram and click 'Start' / send '/start' to permit DMs first.")
            except Exception as e:
                print(f"Error sending prompt to chat {tid}: {e}")
        return results

    def _poll_telegram_selection_multi(self, token: str, prompts: dict, timeout_sec: int = None) -> dict:
        import requests
        import time
        from pathlib import Path
        
        start_time = time.time()
        
        # Load project root and last_update_id.txt to initialize offset
        current = Path(__file__).resolve().parent
        project_root = None
        for parent in [current] + list(current.parents):
            if (parent / "pyproject.toml").exists() or (parent / "knowledge").exists():
                project_root = parent
                break
        if not project_root:
            project_root = Path.cwd()
            
        offset_file = project_root / "last_update_id.txt"
        offset = None
        if offset_file.exists():
            with open(offset_file, "r") as f:
                try:
                    offset = int(f.read().strip()) + 1
                except ValueError:
                    pass
        
        prompt_msg_ids = list(prompts.values())
        print(f"Polling getUpdates for response to messages {prompt_msg_ids} starting with offset {offset}...")
        
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
                                    
                                    # Acknowledge this update on Telegram server to clear it from the queue
                                    requests.get(url, params={"offset": offset, "limit": 1}, timeout=5)
                                    
                                    # Save to file
                                    with open(offset_file, "w") as f:
                                        f.write(str(update_id))
                                    
                                    responder_name = callback.get("from", {}).get("first_name", "Someone")
                                    username = callback.get("from", {}).get("username")
                                    if username:
                                        responder_name = f"{responder_name} (@{username})"
                                    
                                    return {
                                        "data": callback.get("data"),
                                        "responder": html.escape(responder_name),
                                        "winner_chat_id": matched_chat_id,
                                        "winner_msg_id": msg_id
                                    }
            except Exception as e:
                print(f"Error polling Telegram updates: {e}")
            time.sleep(2)
        return None

    def _edit_telegram_message(self, token: str, chat_id: str, message_id: int, new_text: str):
        import requests
        env_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if env_chat_id and str(chat_id) == str(env_chat_id):
            print(f"WARNING: Prevented editing message in group chat {chat_id}")
            return

        url = f"https://api.telegram.org/bot{token}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": new_text,
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload, timeout=10)

    def _edit_telegram_message_with_buttons(self, token: str, chat_id: str, message_id: int, new_text: str, buttons: list):
        import requests
        env_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if env_chat_id and str(chat_id) == str(env_chat_id):
            print(f"WARNING: Prevented editing message in group chat {chat_id}")
            return

        url = f"https://api.telegram.org/bot{token}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": new_text,
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": buttons
            }
        }
        requests.post(url, json=payload, timeout=10)

    @listen(process_messages)
    def save_and_report(self):
        """Write a markdown recommendation report with recommendations."""
        print("\n=== Step 3: Saving State and Generating Report ===")
        provider = os.getenv("SYNC_PROVIDER", "jira").strip().upper()
        
        if self.state.results:
            report_lines = [
                f"# Telegram to {provider} Sync & Recommendations Report",
                f"Total Processed: {self.state.processed_count}\n",
            ]
            for res in self.state.results:
                report_lines.append(f"## Message #{res['message_id']} ({res['sender']})")
                report_lines.append(f"**Original Text**: `{res['text']}`")
                report_lines.append(f"**Sent At**: {res['timestamp']}")
                report_lines.append(f"**Category**: `{res['category']}`")
                report_lines.append(f"**Target {provider} Status**: `{res.get('target_ticket_status')}`\n")
                
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
                        "sender_id": str(message.get('from', {}).get('id', '')),
                        "text": message.get("text") or message.get("caption") or "",
                        "timestamp": str(message.get("date")),
                        "processed": False,
                        "reply_to": reply_data
                    })
        return results

# --- AUTOMATIC TICKETING FOLLOW-UP AGENT SCRIPT ---
def run_followup():
    """Reads pending tickets, runs the knowledge responder to recommend automated follow-ups."""
    provider = os.getenv("SYNC_PROVIDER", "jira").strip().upper()
    print(f"=== Checking {provider} for Pending Tasks needing Follow-up ===")
    
    ticket_client = get_ticket_client()
    try:
        issues = ticket_client.search_issues("", include_comments=True)
    except Exception as e:
        print(f"\nERROR: Failed to fetch open tickets from {provider}: {e}\n", file=sys.stderr)
        raise e
        
    open_issues = []
    for issue in issues:
        fields = issue.get("fields", {})
        status = fields.get("status", {}).get("name", "To Do")
        if status.lower() not in ["done", "resolved", "completed", "closed"]:
            open_issues.append(issue)
            
    if not open_issues:
        print(f"No open tickets found in {provider} requiring follow-up.")
        return
        
    print(f"Found {len(open_issues)} open tickets in {provider}.")
    
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
        goal=f"Analyze an open {provider} ticket, check its status and comment history, and generate a polite follow-up message to send to the provider to nudge progress based on company policy.",
        backstory="You are an active coordinator. You ensure that projects stay on track and providers meet their deadlines. You draft clear follow-up messages.",
        llm=get_llm(),
        allow_delegation=False,
        verbose=True
    )
    
    report = [f"# Proactive {provider} Tasks Follow-up Report\n"]
    
    for issue in open_issues:
        key = issue.get("key")
        fields = issue.get("fields", {})
        summary = fields.get("summary")
        description = fields.get("description")
        status = fields.get("status", {}).get("name")
        comments = fields.get("comment", {}).get("comments", [])
        
        comment_history = "\n".join([f"- Comment: {c.get('body')}" for c in comments])
        
        prompt = f"""
        Ticket Key/ID: {key}
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
        
        report.append(f"## Ticket: [{key}] - {summary}")
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
    provider = os.getenv("SYNC_PROVIDER", "jira").strip().upper()
    
    if not continuous:
        print(f"=== Starting Telegram-to-{provider} Sync Agent (ONE-SHOT MODE) ===")
        flow = TelegramSyncFlow()
        flow.kickoff()
        return

    print(f"=== Starting Telegram-to-{provider} Sync Agent (CONTINUOUS MODE) ===")
    print(f"Polling interval: {poll_interval} seconds. Press Ctrl+C to stop.\n")
    while True:
        try:
            flow = TelegramSyncFlow()
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
    
    provider = os.getenv("SYNC_PROVIDER", "jira").strip().upper()
    
    # Pre-fetch open issues to inject into the LLM context directly
    ticket_client = get_ticket_client()
    open_issues_list = []
    try:
        issues = ticket_client.search_issues("", include_comments=False)
        for issue in issues:
            fields = issue.get("fields", {})
            status = fields.get("status", {}).get("name", "To Do")
            if status.lower() not in ["done", "resolved", "completed", "closed"]:
                open_issues_list.append(f"- [{issue['key']}] {fields.get('summary')} (Status: {status})")
        open_tickets_str = "\n".join(open_issues_list) if open_issues_list else "No open tickets."
    except Exception as e:
        open_tickets_str = "No open tickets (failed to connect)."

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

    # Load dynamic user assignments
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "default")
    assignments = load_user_assignments(chat_id)
    dynamic_assignments_str = ""
    client_names = []
    provider_names = []
    for uid, u in assignments.items():
        if u.get("team") == "client":
            client_names.append(f"{u.get('name')} (ID: {uid})")
        elif u.get("team") == "provider":
            provider_names.append(f"{u.get('name')} (ID: {uid})")
            
    if client_names or provider_names:
        dynamic_assignments_str = "\n\n### Dynamically Assigned Users (supplementing static lists):\n"
        if client_names:
            dynamic_assignments_str += f"- Client Side Senders: {', '.join(client_names)}\n"
        if provider_names:
            dynamic_assignments_str += f"- Provider Side Senders: {', '.join(provider_names)}\n"
            
    msg_company_policies = company_policies + dynamic_assignments_str

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
        "open_tickets": open_tickets_str,
        "company_policies": msg_company_policies,
        "product_specs": product_specs
    }

    primary_name = "Ollama" if os.getenv("OLLAMA_MODEL") else "Gemini"
    print(f"Executing single-stage coordinate_sync_task in simulation with primary {primary_name}...")
    try:
        # Try primary LLM first
        TelegramSyncCrew.use_nvidia = False
        crew_obj = TelegramSyncCrew()
        crew_instance = crew_obj.crew()
        result = crew_instance.kickoff(inputs=inputs)
    except Exception as e:
        # Failover to NVIDIA NIM Llama 3.1 70B
        print(f"Primary {primary_name} execution failed or rate-limited ({e}). Falling back to NVIDIA NIM...")
        TelegramSyncCrew.use_nvidia = True
        crew_obj = TelegramSyncCrew()
        crew_instance = crew_obj.crew()
        result = crew_instance.kickoff(inputs=inputs)

    print("\n=== Agent Results ===")
    print(result.raw)

if __name__ == "__main__":
    kickoff()
