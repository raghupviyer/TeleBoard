import os
import json
import requests
from dotenv import load_dotenv

# Load env variables
load_dotenv(override=True)

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

    def search_issues(self, query: str, include_comments: bool = False) -> list:
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
            issues = data.get("issues", [])
            # Map key and fields for compatibility
            results = []
            for issue in issues:
                fields = issue.get("fields", {})
                comments = fields.get("comment", {}).get("comments", [])
                results.append({
                    "key": issue.get("key"),
                    "id": issue.get("id"),
                    "fields": {
                        "summary": fields.get("summary"),
                        "description": fields.get("description"),
                        "status": {
                            "name": fields.get("status", {}).get("name")
                        },
                        "comment": {
                            "comments": [{"body": c.get("body")} for c in comments]
                        }
                    }
                })
            return results
        except Exception as e:
            print(f"[Jira] Error searching issues with JQL '{jql}': {e}")
            raise e

    def create_issue(self, summary: str, description: str, issuetype: str = "Bug") -> dict:
        # Clean summary to prevent any newline/carriage return characters or excessive spaces
        cleaned_summary = " ".join(summary.split()).strip()
        if len(cleaned_summary) > 250:
            cleaned_summary = cleaned_summary[:247] + "..."

        url = f"{self.host}/rest/api/2/issue"
        payload = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": cleaned_summary,
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
                
            return {
                "key": issue_data.get("key"),
                "id": issue_data.get("id"),
                "fields": {
                    "summary": summary,
                    "description": description,
                    "status": {"name": "To Do"}
                }
            }
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
            data = response.json()
            fields = data.get("fields", {})
            comments = fields.get("comment", {}).get("comments", [])
            return {
                "key": data.get("key"),
                "id": data.get("id"),
                "fields": {
                    "summary": fields.get("summary"),
                    "description": fields.get("description"),
                    "status": {
                        "name": fields.get("status", {}).get("name")
                    },
                    "comment": {
                        "comments": [{"body": c.get("body")} for c in comments]
                    }
                }
            }
        except Exception as e:
            print(f"[Jira] Error getting issue {issue_key}: {e}")
            raise e

    def transition_issue(self, issue_key: str, status_name: str) -> bool:
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


class OpenProjectMCPClient:
    def __init__(self):
        self.host = os.getenv("OPENPROJECT_HOST", "").rstrip("/")
        self.token = os.getenv("OPENPROJECT_API_KEY", "")
        self.project_id = os.getenv("OPENPROJECT_PROJECT_ID", "").strip('"')

        # Raise error if OpenProject credentials are missing
        if not self.host or not self.token:
            raise ValueError(
                "\n========================================================================\n"
                "CONFIGURATION ERROR: OpenProject API credentials are missing in your .env file.\n"
                "Please set the following keys:\n"
                "  - OPENPROJECT_HOST (e.g. https://your-openproject-instance.com)\n"
                "  - OPENPROJECT_API_KEY (your personal access token)\n"
                "  - OPENPROJECT_PROJECT_ID (identifier or numeric ID of the project)\n"
                "========================================================================\n"
            )
        
        self.auth = ("apikey", self.token)
        self.headers = {
            "Accept": "application/hal+json",
            "Content-Type": "application/json"
        }
        
        self.project_href = f"/api/v3/projects/{self.project_id}"
        self.type_mappings = {}
        self.status_mappings = {}
        
        # Initialize mappings and resolve project keys
        self._resolve_project_key()
        self._resolve_types()
        self._resolve_statuses()

    def _resolve_project_key(self):
        try:
            url = f"{self.host}/api/v3/projects"
            response = requests.get(url, auth=self.auth, headers=self.headers, timeout=10)
            if response.status_code == 200:
                projects = response.json().get("_embedded", {}).get("elements", [])
                for p in projects:
                    if (p.get("identifier") == self.project_id or 
                        p.get("name") == self.project_id or 
                        str(p.get("id")) == str(self.project_id)):
                        print(f"[OpenProject] Resolved project '{self.project_id}' to identifier '{p.get('identifier')}' and ID '{p.get('id')}'")
                        self.project_id = p.get("identifier")
                        self.project_href = p.get("_links", {}).get("self", {}).get("href")
                        break
        except Exception as e:
            print(f"[OpenProject] Error resolving project: {e}")

    def _resolve_types(self):
        try:
            url = f"{self.host}/api/v3/types"
            response = requests.get(url, auth=self.auth, headers=self.headers, timeout=10)
            if response.status_code == 200:
                types = response.json().get("_embedded", {}).get("elements", [])
                for t in types:
                    name = t.get("name", "").lower()
                    href = t.get("_links", {}).get("self", {}).get("href")
                    self.type_mappings[name] = href
                print(f"[OpenProject] Resolved types: {list(self.type_mappings.keys())}")
        except Exception as e:
            print(f"[OpenProject] Error resolving types: {e}")

    def _resolve_statuses(self):
        try:
            url = f"{self.host}/api/v3/statuses"
            response = requests.get(url, auth=self.auth, headers=self.headers, timeout=10)
            if response.status_code == 200:
                statuses = response.json().get("_embedded", {}).get("elements", [])
                for s in statuses:
                    name = s.get("name", "").lower()
                    href = s.get("_links", {}).get("self", {}).get("href")
                    self.status_mappings[name] = href
                print(f"[OpenProject] Resolved statuses: {list(self.status_mappings.keys())}")
        except Exception as e:
            print(f"[OpenProject] Error resolving statuses: {e}")

    def _translate_work_package(self, wp: dict, include_comments: bool = False) -> dict:
        wp_id = wp.get("id")
        subject = wp.get("subject", "No Subject")
        desc_obj = wp.get("description", {})
        description = desc_obj.get("raw", "") if isinstance(desc_obj, dict) else str(desc_obj)
        
        status_link = wp.get("_links", {}).get("status", {})
        status_name = status_link.get("title", "To Do")
        
        comments = []
        if include_comments and wp_id:
            comments = self.get_comments(wp_id)
                
        return {
            "key": str(wp_id),
            "id": wp_id,
            "fields": {
                "summary": subject,
                "description": description,
                "status": {
                    "name": status_name
                },
                "comment": {
                    "comments": [{"body": c} for c in comments]
                }
            }
        }

    def get_comments(self, wp_id: int) -> list:
        url = f"{self.host}/api/v3/work_packages/{wp_id}/activities"
        try:
            response = requests.get(url, auth=self.auth, headers=self.headers, timeout=10)
            if response.status_code == 200:
                elements = response.json().get("_embedded", {}).get("elements", [])
                comments = []
                for element in elements:
                    comment_obj = element.get("comment", {})
                    if comment_obj and isinstance(comment_obj, dict):
                        raw_text = comment_obj.get("raw")
                        if raw_text:
                            comments.append(raw_text)
                return comments
        except Exception as e:
            print(f"[OpenProject] Error fetching activities for work package {wp_id}: {e}")
        return []

    def search_issues(self, query: str, include_comments: bool = False) -> list:
        filters = [{"status": {"operator": "o", "values": []}}]
        if query:
            filters.append({"subjectOrId": {"operator": "**", "values": [query]}})
            
        url = f"{self.host}/api/v3/projects/{self.project_id}/work_packages"
        params = {
            "filters": json.dumps(filters),
            "pageSize": 50
        }
        try:
            response = requests.get(url, auth=self.auth, headers=self.headers, params=params, timeout=15)
            response.raise_for_status()
            elements = response.json().get("_embedded", {}).get("elements", [])
            translated = []
            for wp in elements:
                translated.append(self._translate_work_package(wp, include_comments=include_comments))
            return translated
        except Exception as e:
            print(f"[OpenProject] Error searching work packages: {e}")
            raise e

    def create_issue(self, summary: str, description: str, issuetype: str = "Bug") -> dict:
        # Clean summary to prevent any newline/carriage return characters or excessive spaces
        cleaned_summary = " ".join(summary.split()).strip()
        if len(cleaned_summary) > 250:
            cleaned_summary = cleaned_summary[:247] + "..."

        type_href = self.type_mappings.get(issuetype.lower())
        if not type_href:
            type_href = list(self.type_mappings.values())[0] if self.type_mappings else f"/api/v3/types/1"
            
        url = f"{self.host}/api/v3/work_packages"
        payload = {
            "subject": cleaned_summary,
            "description": {
                "format": "markdown",
                "raw": description
            },
            "_links": {
                "project": {"href": self.project_href},
                "type": {"href": type_href}
            }
        }
        try:
            response = requests.post(url, auth=self.auth, headers=self.headers, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
            return self._translate_work_package(data)
        except Exception as e:
            print(f"[OpenProject] Error creating work package: {e}")
            raise e

    def add_comment(self, issue_key: str, body: str) -> dict:
        url = f"{self.host}/api/v3/work_packages/{issue_key}/activities"
        payload = {
            "comment": {
                "format": "markdown",
                "raw": body
            }
        }
        try:
            response = requests.post(url, auth=self.auth, headers=self.headers, json=payload, timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"[OpenProject] Error adding comment to work package {issue_key}: {e}")
            raise e

    def get_issue(self, issue_key: str) -> dict:
        url = f"{self.host}/api/v3/work_packages/{issue_key}"
        try:
            response = requests.get(url, auth=self.auth, headers=self.headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            return self._translate_work_package(data, include_comments=True)
        except Exception as e:
            print(f"[OpenProject] Error getting work package {issue_key}: {e}")
            raise e

    def transition_issue(self, issue_key: str, status_name: str) -> bool:
        target = status_name.strip().lower()
        status_href = None
        
        # Direct match
        if target in self.status_mappings:
            status_href = self.status_mappings[target]
        else:
            # Substring match
            for name, href in self.status_mappings.items():
                if target in name or name in target:
                    status_href = href
                    break
                    
        if not status_href:
            print(f"[OpenProject] Transition status '{status_name}' not found. Available: {list(self.status_mappings.keys())}")
            return False
            
        try:
            url = f"{self.host}/api/v3/work_packages/{issue_key}"
            get_resp = requests.get(url, auth=self.auth, headers=self.headers, timeout=10)
            get_resp.raise_for_status()
            wp_data = get_resp.json()
            lock_version = wp_data.get("lockVersion", 1)
        except Exception as e:
            print(f"[OpenProject] Error getting lockVersion for work package {issue_key}: {e}")
            return False
            
        patch_url = f"{self.host}/api/v3/work_packages/{issue_key}"
        payload = {
            "lockVersion": lock_version,
            "_links": {
                "status": {
                    "href": status_href
                }
            }
        }
        try:
            patch_resp = requests.patch(patch_url, auth=self.auth, headers=self.headers, json=payload, timeout=15)
            patch_resp.raise_for_status()
            print(f"[OpenProject] Successfully transitioned work package {issue_key} to status '{status_name}' ({status_href})")
            return True
        except Exception as e:
            print(f"[OpenProject] Error transitioning work package {issue_key} to '{status_name}': {e}")
            return False


def get_ticket_client():
    """Factory to instantiate the client based on SYNC_PROVIDER environment variable."""
    provider = os.getenv("SYNC_PROVIDER", "jira").strip().lower()
    if provider == "openproject":
        print("[Client Factory] Instantiating OpenProjectMCPClient")
        return OpenProjectMCPClient()
    else:
        print("[Client Factory] Instantiating JiraMCPClient")
        return JiraMCPClient()
