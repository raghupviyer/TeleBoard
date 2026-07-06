import os
import requests
from dotenv import load_dotenv

# Load env variables
load_dotenv()

class JiraMCPClient:
    def __init__(self):
        self.host = os.getenv("JIRA_HOST", "").rstrip("/")
        self.email = os.getenv("JIRA_EMAIL", "")
        self.token = os.getenv("JIRA_API_TOKEN", "")
        self.project_key = os.getenv("JIRA_PROJECT_KEY", "BUG").strip('"')

        # Raise error if Jira credentials are missing
        if not self.host or not self.email or not self.token:
            raise ValueError(
                "\n========================================================================\n"
                "CONFIGURATION ERROR: Jira API credentials are missing in your .env file.\n"
                "Please set the following keys:\n"
                "  - JIRA_HOST (e.g. https://your-domain.atlassian.net)\n"
                "  - JIRA_EMAIL (your Atlassian email)\n"
                "  - JIRA_API_TOKEN (your Atlassian developer API token)\n"
                "========================================================================\n"
            )
        
        self.auth = (self.email, self.token)
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        
        # Resolve full project name (e.g. "Tiny Trader") to the actual short key (e.g. "SCRUM")
        self._resolve_project_key()

    def _resolve_project_key(self):
        try:
            url = f"{self.host}/rest/api/3/project"
            response = requests.get(url, auth=self.auth, headers=self.headers, timeout=10)
            if response.status_code == 200:
                projects = response.json()
                for p in projects:
                    if p.get("key") == self.project_key or p.get("name") == self.project_key:
                        print(f"[Jira] Resolved project name/key '{self.project_key}' to key '{p.get('key')}'")
                        self.project_key = p.get("key")
                        break
        except Exception as e:
            print(f"[Jira] Error resolving project key: {e}")

    def search_issues(self, query: str) -> list:
        # If query is empty, find open issues
        if not query:
            jql = f'project = "{self.project_key}" AND statusCategory != Done'
        else:
            jql = f'project = "{self.project_key}" AND (summary ~ "{query}" OR description ~ "{query}")'
            
        url = f"{self.host}/rest/api/3/search/jql"
        params = {
            "jql": jql,
            "maxResults": 50,
            "fields": "summary,status,description,comment,key"
        }
        try:
            response = requests.get(url, auth=self.auth, headers=self.headers, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            return data.get("issues", [])
        except Exception as e:
            print(f"[Jira] Error searching issues with JQL '{jql}': {e}")
            raise e

    def create_issue(self, summary: str, description: str, issuetype: str = "Bug") -> dict:
        url = f"{self.host}/rest/api/2/issue"
        payload = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": summary,
                "description": description,
                "issuetype": {"name": issuetype}
            }
        }
        response = None
        try:
            response = requests.post(url, auth=self.auth, headers=self.headers, json=payload, timeout=15)
            response.raise_for_status()
            issue_data = response.json()
            
            # Dynamically assign to default sprint if configured in env
            default_sprint = os.getenv("JIRA_DEFAULT_SPRINT_ID", "")
            if default_sprint and issue_data.get("key"):
                self._assign_issue_to_sprint(issue_data["key"], default_sprint)
                
            return issue_data
        except Exception as e:
            print(f"[Jira] Error creating issue: {e}")
            if response is not None:
                print(f"[Jira] Error response body: {response.text}")
            raise e

    def _assign_issue_to_sprint(self, issue_key: str, sprint_id: str):
        url = f"{self.host}/rest/agile/1.0/sprint/{sprint_id}/issue"
        payload = {
            "issues": [issue_key]
        }
        try:
            resp = requests.post(url, auth=self.auth, headers=self.headers, json=payload, timeout=10)
            if resp.status_code == 204:
                print(f"[Jira] Successfully assigned new issue {issue_key} to sprint {sprint_id}")
            else:
                print(f"[Jira] Failed to assign issue {issue_key} to sprint {sprint_id}: {resp.status_code} {resp.text}")
        except Exception as e:
            print(f"[Jira] Error assigning issue {issue_key} to sprint {sprint_id}: {e}")

    def add_comment(self, issue_key: str, body: str) -> dict:
        url = f"{self.host}/rest/api/2/issue/{issue_key}/comment"
        payload = {
            "body": body
        }
        try:
            response = requests.post(url, auth=self.auth, headers=self.headers, json=payload, timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"[Jira] Error adding comment: {e}")
            raise e

    def get_issue(self, issue_key: str) -> dict:
        url = f"{self.host}/rest/api/2/issue/{issue_key}"
        try:
            response = requests.get(url, auth=self.auth, headers=self.headers, timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"[Jira] Error getting issue {issue_key}: {e}")
            raise e

    def transition_issue(self, issue_key: str, status_name: str) -> bool:
        """
        Transition an issue to a target status name (e.g. 'In Progress', 'In Review', 'To Do').
        Queries the available transitions for the issue and matches by name.
        """
        url = f"{self.host}/rest/api/2/issue/{issue_key}/transitions"
        try:
            response = requests.get(url, auth=self.auth, headers=self.headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            transitions = data.get("transitions", [])
            
            transition_id = None
            matched_name = None
            target = status_name.strip().lower()
            
            # Direct match
            for t in transitions:
                name = t.get("name", "").strip().lower()
                to_name = t.get("to", {}).get("name", "").strip().lower()
                if to_name == target or name == target:
                    transition_id = t.get("id")
                    matched_name = t.get("to", {}).get("name") or t.get("name")
                    break
            
            # Substring match fallback
            if not transition_id:
                for t in transitions:
                    name = t.get("name", "").strip().lower()
                    to_name = t.get("to", {}).get("name", "").strip().lower()
                    if target in to_name or target in name:
                        transition_id = t.get("id")
                        matched_name = t.get("to", {}).get("name") or t.get("name")
                        break
                        
            if not transition_id:
                avail = [f"{t.get('name')} (to: {t.get('to', {}).get('name')})" for t in transitions]
                print(f"[Jira] Transition to '{status_name}' not found for {issue_key}. Available: {avail}")
                return False
                
            payload = {
                "transition": {
                    "id": transition_id
                }
            }
            post_url = f"{self.host}/rest/api/2/issue/{issue_key}/transitions"
            post_resp = requests.post(post_url, auth=self.auth, headers=self.headers, json=payload, timeout=15)
            post_resp.raise_for_status()
            print(f"[Jira] Successfully transitioned {issue_key} to '{matched_name}'")
            return True
        except Exception as e:
            print(f"[Jira] Error transitioning issue {issue_key} to '{status_name}': {e}")
            return False
