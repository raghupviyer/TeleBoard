from typing import Type, List, Dict, Any
from crewai.tools import BaseTool
from pydantic import BaseModel, Field
from tele_to_trello.utils.mcp_client import OpenProjectMCPClient

# Initialize client globally or inside the tools
openproject_client = OpenProjectMCPClient()

# --- Search Work Package Tool ---
class SearchOpenProjectInput(BaseModel):
    query: str = Field(..., description="Search query string to search by work package ID or subject.")

class SearchOpenProjectTool(BaseTool):
    name: str = "search_openproject_work_packages"
    description: str = "Search for existing work packages in OpenProject. Use this to find if a Telegram message relates to an existing ticket."
    args_schema: Type[BaseModel] = SearchOpenProjectInput

    def _run(self, query: str) -> str:
        try:
            issues = openproject_client.search_issues(query)
            if not issues:
                return "No matching OpenProject work packages found."
            
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
            return f"Error searching OpenProject work packages: {e}"

# --- Create Work Package Tool ---
class CreateOpenProjectInput(BaseModel):
    summary: str = Field(..., description="A short, descriptive summary of the bug or feature request.")
    description: str = Field(..., description="Detailed description of the bug or feature request, including chat history or context.")
    issuetype: str = Field("Bug", description="The type of work package: 'Bug' (for bugs) or 'Task' (for features).")

class CreateOpenProjectTool(BaseTool):
    name: str = "create_openproject_work_package"
    description: str = "Create a new bug ticket or feature request in OpenProject. Returns the ID of the newly created work package."
    args_schema: Type[BaseModel] = CreateOpenProjectInput

    def _run(self, summary: str, description: str, issuetype: str = "Bug") -> str:
        try:
            issue = openproject_client.create_issue(summary, description, issuetype)
            key = issue.get("key", "Unknown")
            return f"Successfully created work package {key} ({issuetype}): '{summary}'"
        except Exception as e:
            return f"Error creating OpenProject work package: {e}"

# --- Add Comment Tool ---
class AddOpenProjectCommentInput(BaseModel):
    issue_key: str = Field(..., description="The OpenProject work package ID to add the comment under.")
    body: str = Field(..., description="The content of the comment. Include message sender, content and timestamp.")

class AddOpenProjectCommentTool(BaseTool):
    name: str = "add_openproject_comment"
    description: str = "Add a comment to an existing OpenProject work package. Use this to append conversation history or replies from the group chat."
    args_schema: Type[BaseModel] = AddOpenProjectCommentInput

    def _run(self, issue_key: str, body: str) -> str:
        try:
            openproject_client.add_comment(issue_key, body)
            return f"Successfully added comment to work package {issue_key}."
        except Exception as e:
            return f"Error adding comment to OpenProject work package: {e}"
