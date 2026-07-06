from typing import Type, List, Dict, Any
from crewai.tools import BaseTool
from pydantic import BaseModel, Field
from tele_to_trello.utils.mcp_client import JiraMCPClient

# Initialize client globally or inside the tools
jira_client = JiraMCPClient()

# --- Search Issue Tool ---
class SearchJiraIssuesInput(BaseModel):
    query: str = Field(..., description="Search query string to search by issue key, summary, or description.")

class SearchJiraIssuesTool(BaseTool):
    name: str = "search_jira_issues"
    description: str = "Search for existing issues in Jira. Use this to find if a Telegram message relates to an existing ticket."
    args_schema: Type[BaseModel] = SearchJiraIssuesInput

    def _run(self, query: str) -> str:
        try:
            issues = jira_client.search_issues(query)
            if not issues:
                return "No matching Jira issues found."
            
            output = []
            for issue in issues:
                key = issue.get("key", "N/A")
                fields = issue.get("fields", {})
                summary = fields.get("summary", "No Summary")
                status = fields.get("status", {}).get("name", "N/A")
                desc = fields.get("description", "No Description")
                output.append(f"- [{key}] Status: {status} | Summary: {summary}\n  Description: {desc}")
            
            return "\n".join(output)
        except Exception as e:
            return f"Error searching Jira issues: {e}"

# --- Create Issue Tool ---
class CreateJiraIssueInput(BaseModel):
    summary: str = Field(..., description="A short, descriptive summary of the bug or feature request.")
    description: str = Field(..., description="Detailed description of the bug or feature request, including chat history or context.")
    issuetype: str = Field("Bug", description="The type of Jira issue: 'Bug' (for bugs) or 'Task' (for features).")

class CreateJiraIssueTool(BaseTool):
    name: str = "create_jira_issue"
    description: str = "Create a new bug ticket or feature request in Jira. Returns the key of the newly created issue."
    args_schema: Type[BaseModel] = CreateJiraIssueInput

    def _run(self, summary: str, description: str, issuetype: str = "Bug") -> str:
        try:
            issue = jira_client.create_issue(summary, description, issuetype)
            key = issue.get("key", "Unknown")
            return f"Successfully created issue {key} ({issuetype}): '{summary}'"
        except Exception as e:
            return f"Error creating Jira issue: {e}"

# --- Add Comment Tool ---
class AddJiraCommentInput(BaseModel):
    issue_key: str = Field(..., description="The Jira issue key (e.g. BUG-1) or summary to add the comment under.")
    body: str = Field(..., description="The content of the comment. Include message sender, content and timestamp.")

class AddJiraCommentTool(BaseTool):
    name: str = "add_jira_comment"
    description: str = "Add a comment to an existing Jira issue. Use this to append conversation history or replies from the group chat."
    args_schema: Type[BaseModel] = AddJiraCommentInput

    def _run(self, issue_key: str, body: str) -> str:
        try:
            jira_client.add_comment(issue_key, body)
            return f"Successfully added comment to issue {issue_key}."
        except Exception as e:
            return f"Error adding comment to Jira issue: {e}"
