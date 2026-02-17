#!/usr/bin/env python3
"""
Jira MCP Server

A Model Context Protocol server that provides integration with Jira.
Allows AI assistants to interact with Jira issues, projects, and workflows.
"""

import asyncio
import json
import logging
import os
import statistics
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from dotenv import load_dotenv
from jira import JIRA
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    TextContent,
    Tool,
    ServerCapabilities,
)

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Status categorization for cycle time analysis (case-insensitive matching)
ACTIVE_STATUSES = {'in progress', 'coding in progress', 'in development', 'in review',
                   'review', 'code review', 'qa', 'qa in progress', 'testing'}
DONE_STATUSES = {'closed', 'done', 'resolved', 'verified', 'release pending'}
BACKLOG_STATUSES = {'new', 'open', 'backlog', 'to do', 'refinement', 'planning'}

class JiraMCPServer:
    def __init__(self):
        self.server = Server("jira-mcp-server")
        self.jira_client: Optional[JIRA] = None
        self._setup_tools()
        
    def _setup_tools(self):
        """Set up all available tools"""
        
        @self.server.list_tools()
        async def list_tools() -> List[Tool]:
            """List all available Jira tools"""
            return [
                Tool(
                    name="get_issue",
                    description="Get detailed information about a specific Jira issue",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "The Jira issue key (e.g., PROJ-123)"
                            }
                        },
                        "required": ["issue_key"]
                    }
                ),
                Tool(
                    name="search_issues",
                    description="Search for Jira issues using JQL (Jira Query Language)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "jql": {
                                "type": "string",
                                "description": "JQL query string (e.g., 'project = PROJ AND status = Open')"
                            },
                            "max_results": {
                                "type": "integer",
                                "description": "Maximum number of results to return",
                                "default": 50
                            }
                        },
                        "required": ["jql"]
                    }
                ),
                Tool(
                    name="create_issue",
                    description="Create a new Jira issue",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_key": {
                                "type": "string",
                                "description": "Project key (e.g., PROJ)"
                            },
                            "issue_type": {
                                "type": "string",
                                "description": "Issue type (e.g., Task, Bug, Story, Epic)"
                            },
                            "summary": {
                                "type": "string",
                                "description": "Issue title/summary"
                            },
                            "description": {
                                "type": "string",
                                "description": "Issue description"
                            },
                            "priority": {
                                "type": "string",
                                "description": "Priority level (e.g., Blocker, Critical, Major, Minor, Normal, Undefined)",
                                "default": "Normal"
                            },
                            "due_date": {
                                "type": "string",
                                "description": "Due date in YYYY-MM-DD format (optional)"
                            },
                            "epic_name": {
                                "type": "string",
                                "description": "Epic name (required when issue_type is Epic)"
                            }
                        },
                        "required": ["project_key", "issue_type", "summary", "description"]
                    }
                ),
                Tool(
                    name="update_issue",
                    description="Update an existing Jira issue",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "The Jira issue key"
                            },
                            "summary": {
                                "type": "string",
                                "description": "New summary (optional)"
                            },
                            "description": {
                                "type": "string",
                                "description": "New description (optional)"
                            },
                            "story_points": {
                                "type": "number",
                                "description": "Story points estimate (optional)"
                            },
                            "priority": {
                                "type": "string",
                                "description": "Priority level (e.g., Blocker, Critical, Major, Minor, Normal, Undefined)"
                            },
                            "assignee": {
                                "type": "string",
                                "description": "Assignee email, account ID, or 'me'/'myself' for current user (optional)"
                            },
                            "security_level": {
                                "type": "string",
                                "description": "Security level name or ID (optional, e.g., 'Red Hat Employee', 'Team'). Use empty string to remove security level."
                            }
                        },
                        "required": ["issue_key"]
                    }
                ),
                Tool(
                    name="add_comment",
                    description="Add a comment to a Jira issue",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "The Jira issue key"
                            },
                            "comment": {
                                "type": "string",
                                "description": "Comment text"
                            },
                            "security_level": {
                                "type": "string",
                                "description": "Security level name or ID (optional, e.g., 'Employee', 'Internal')"
                            }
                        },
                        "required": ["issue_key", "comment"]
                    }
                ),
                Tool(
                    name="get_comments",
                    description="Get all comments for a Jira issue",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "The Jira issue key"
                            }
                        },
                        "required": ["issue_key"]
                    }
                ),
                Tool(
                    name="transition_issue",
                    description="Move an issue through workflow states",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "The Jira issue key"
                            },
                            "transition_name": {
                                "type": "string",
                                "description": "Name of the transition (e.g., 'In Progress', 'Done')"
                            }
                        },
                        "required": ["issue_key", "transition_name"]
                    }
                ),
                Tool(
                    name="get_project",
                    description="Get information about a Jira project",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_key": {
                                "type": "string",
                                "description": "Project key"
                            }
                        },
                        "required": ["project_key"]
                    }
                ),
                Tool(
                    name="get_issue_types",
                    description="Get available issue types for a project",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_key": {
                                "type": "string",
                                "description": "Project key"
                            }
                        },
                        "required": ["project_key"]
                    }
                ),
                Tool(
                    name="get_my_issues",
                    description="Get issues assigned to the current user",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "max_results": {
                                "type": "integer",
                                "description": "Maximum number of results to return",
                                "default": 20
                            }
                        }
                    }
                ),
                Tool(
                    name="get_project_issues",
                    description="Get all issues for a specific project",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_key": {
                                "type": "string",
                                "description": "Project key"
                            },
                            "max_results": {
                                "type": "integer",
                                "description": "Maximum number of results to return",
                                "default": 50
                            }
                        },
                        "required": ["project_key"]
                    }
                ),
                Tool(
                    name="set_sprint",
                    description="Set the sprint for a Jira issue. Can set to current sprint, next sprint, a specific sprint by name/ID, or remove the sprint entirely.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "The Jira issue key"
                            },
                            "sprint_option": {
                                "type": "string",
                                "description": "Sprint selection option: 'current' for current active sprint, 'next' for next planned sprint, 'specific' to specify a sprint by name/ID, or 'none' to remove the sprint",
                                "enum": ["current", "next", "specific", "none"]
                            },
                            "sprint_value": {
                                "type": "string",
                                "description": "Sprint name or ID (required only when sprint_option is 'specific')"
                            },
                            "board_id": {
                                "type": "integer",
                                "description": "Board ID to search for sprints (optional, will auto-detect if not provided)"
                            }
                        },
                        "required": ["issue_key", "sprint_option"]
                    }
                ),
                Tool(
                    name="set_epic_link",
                    description="Set or remove the epic link for a Jira issue. Links an issue to an epic or removes the epic link.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "The Jira issue key to update"
                            },
                            "epic_key": {
                                "type": "string",
                                "description": "The epic issue key to link to (e.g., PROJ-123), or null/empty string to remove the epic link"
                            }
                        },
                        "required": ["issue_key"]
                    }
                ),
                Tool(
                    name="get_components",
                    description="Get available components for a project",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_key": {
                                "type": "string",
                                "description": "Project key"
                            }
                        },
                        "required": ["project_key"]
                    }
                ),
                Tool(
                    name="set_components",
                    description="Set components for a Jira issue. Replaces existing components with the provided list.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "The Jira issue key"
                            },
                            "components": {
                                "type": "array",
                                "items": {
                                    "type": "string"
                                },
                                "description": "List of component names to set. Use empty array to remove all components."
                            }
                        },
                        "required": ["issue_key", "components"]
                    }
                ),
                Tool(
                    name="get_issue_sprint_history",
                    description="Get the history of sprint changes for an issue. Shows when the issue was added to or removed from sprints.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "The Jira issue key (e.g., PROJ-123)"
                            }
                        },
                        "required": ["issue_key"]
                    }
                ),
                Tool(
                    name="analyze_sprint_scope",
                    description="Analyze a sprint to identify planned vs added issues, punted issues, and calculate predictability. Uses Jira's sprint report API for accurate data including removed/punted issues.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "sprint_name": {
                                "type": "string",
                                "description": "The sprint name to analyze"
                            },
                            "board_id": {
                                "type": "integer",
                                "description": "Board ID (optional, will auto-detect if not provided)"
                            }
                        },
                        "required": ["sprint_name"]
                    }
                ),
                Tool(
                    name="get_issue_cycle_time",
                    description="Get the cycle time and status transition timeline for a single issue. Shows time from first active status (In Progress) to last done status (Closed), with time spent in each status.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "The Jira issue key (e.g., PROJ-123)"
                            }
                        },
                        "required": ["issue_key"]
                    }
                ),
                Tool(
                    name="analyze_cycle_time",
                    description="Analyze cycle time statistics for completed issues in a date range or sprint. Shows median, average, 85th percentile cycle times, breakdown by issue type, and flags outliers. Provide either start_date+end_date or sprint_name.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "start_date": {
                                "type": "string",
                                "description": "Start date for resolution range (YYYY-MM-DD). Issues resolved on or after this date are included."
                            },
                            "end_date": {
                                "type": "string",
                                "description": "End date for resolution range (YYYY-MM-DD). Issues resolved before this date are included."
                            },
                            "team": {
                                "type": "string",
                                "description": "Filter by AssignedTeam value (optional, e.g., 'rhos-connectivity-neutron-gluon')"
                            },
                            "sprint_name": {
                                "type": "string",
                                "description": "Sprint name to analyze (optional, alternative to date range)"
                            },
                            "board_id": {
                                "type": "integer",
                                "description": "Board ID (optional, only used with sprint_name)"
                            }
                        }
                    }
                )
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
            """Handle tool calls"""
            
            # Initialize Jira client if not already done
            if not self.jira_client:
                await self._init_jira_client()
            
            try:
                if name == "get_issue":
                    return await self._get_issue(arguments["issue_key"])
                elif name == "search_issues":
                    return await self._search_issues(
                        arguments["jql"],
                        arguments.get("max_results", 50)
                    )
                elif name == "create_issue":
                    return await self._create_issue(
                        arguments["project_key"],
                        arguments["issue_type"],
                        arguments["summary"],
                        arguments["description"],
                        arguments.get("priority", "Normal"),
                        arguments.get("due_date"),
                        arguments.get("epic_name")
                    )
                elif name == "update_issue":
                    return await self._update_issue(
                        arguments["issue_key"],
                        arguments.get("summary"),
                        arguments.get("description"),
                        arguments.get("story_points"),
                        arguments.get("priority"),
                        arguments.get("assignee"),
                        arguments.get("security_level")
                    )
                elif name == "add_comment":
                    return await self._add_comment(
                        arguments["issue_key"],
                        arguments["comment"],
                        arguments.get("security_level")
                    )
                elif name == "get_comments":
                    return await self._get_comments(arguments["issue_key"])
                elif name == "transition_issue":
                    return await self._transition_issue(
                        arguments["issue_key"],
                        arguments["transition_name"]
                    )
                elif name == "get_project":
                    return await self._get_project(arguments["project_key"])
                elif name == "get_issue_types":
                    return await self._get_issue_types(arguments["project_key"])
                elif name == "get_my_issues":
                    return await self._get_my_issues(arguments.get("max_results", 20))
                elif name == "get_project_issues":
                    return await self._get_project_issues(
                        arguments["project_key"],
                        arguments.get("max_results", 50)
                    )
                elif name == "set_sprint":
                    return await self._set_sprint(
                        arguments["issue_key"],
                        arguments["sprint_option"],
                        arguments.get("sprint_value"),
                        arguments.get("board_id")
                    )
                elif name == "set_epic_link":
                    return await self._set_epic_link(
                        arguments["issue_key"],
                        arguments.get("epic_key")
                    )
                elif name == "get_components":
                    return await self._get_components(arguments["project_key"])
                elif name == "set_components":
                    return await self._set_components(
                        arguments["issue_key"],
                        arguments["components"]
                    )
                elif name == "get_issue_sprint_history":
                    return await self._get_issue_sprint_history(arguments["issue_key"])
                elif name == "analyze_sprint_scope":
                    return await self._analyze_sprint_scope(
                        arguments["sprint_name"],
                        arguments.get("board_id")
                    )
                elif name == "get_issue_cycle_time":
                    return await self._get_issue_cycle_time(arguments["issue_key"])
                elif name == "analyze_cycle_time":
                    return await self._analyze_cycle_time(
                        start_date=arguments.get("start_date"),
                        end_date=arguments.get("end_date"),
                        team=arguments.get("team"),
                        sprint_name=arguments.get("sprint_name"),
                        board_id=arguments.get("board_id")
                    )
                else:
                    return [TextContent(type="text", text=f"Unknown tool: {name}")]
                    
            except Exception as e:
                logger.error(f"Error calling tool {name}: {e}")
                return [TextContent(type="text", text=f"Error: {str(e)}")]

    async def _init_jira_client(self):
        """Initialize the Jira client with credentials"""
        try:
            server = os.getenv("JIRA_SERVER")
            email = os.getenv("JIRA_EMAIL")
            api_token = os.getenv("JIRA_API_TOKEN")
            
            if not server or not email or not api_token:
                raise ValueError("Missing required environment variables: JIRA_SERVER, JIRA_EMAIL, JIRA_API_TOKEN")
            
            from jira.client import TokenAuth
            self.jira_client = JIRA(
                server=server,
                token_auth=api_token
            )
            logger.info("Successfully connected to Jira")
            
        except Exception as e:
            logger.error(f"Failed to initialize Jira client: {e}")
            raise

    async def _get_issue(self, issue_key: str) -> List[TextContent]:
        """Get detailed information about a Jira issue"""
        try:
            if not self.jira_client:
                return [TextContent(type="text", text="Jira client not initialized")]

            issue = self.jira_client.issue(issue_key)

            # Try to find sprint information
            sprint_info = "No sprint"
            all_fields = self.jira_client.fields()
            sprint_field = None
            for field in all_fields:
                if field.get('name', '').lower() == 'sprint':
                    sprint_field = field['id']
                    break

            # Fallback to common sprint field IDs if not found by name
            if not sprint_field:
                for candidate in ['customfield_12310940', 'customfield_10020', 'customfield_10010']:
                    if hasattr(issue.fields, candidate):
                        sprint_field = candidate
                        break

            if sprint_field and hasattr(issue.fields, sprint_field):
                sprint_data = getattr(issue.fields, sprint_field)
                if sprint_data:
                    if isinstance(sprint_data, list) and len(sprint_data) > 0:
                        # Get the last (current) sprint
                        sprint = sprint_data[-1]
                        if hasattr(sprint, 'name'):
                            sprint_info = sprint.name
                        else:
                            # Sprint might be a string, try to parse it
                            sprint_str = str(sprint)
                            # Extract name from string format: "com.atlassian.greenhopper.service.sprint.Sprint@...[name=Sprint Name,...]"
                            if 'name=' in sprint_str:
                                name_start = sprint_str.find('name=') + 5
                                name_end = sprint_str.find(',', name_start)
                                if name_end == -1:
                                    name_end = sprint_str.find(']', name_start)
                                sprint_info = sprint_str[name_start:name_end]
                    elif hasattr(sprint_data, 'name'):
                        sprint_info = sprint_data.name

            # Try to find epic link information
            epic_link_info = "No epic link"
            epic_link_field = None
            for field in all_fields:
                if field.get('name', '').lower() == 'epic link':
                    epic_link_field = field['id']
                    break

            # Fallback to common epic link field IDs if not found by name
            if not epic_link_field:
                for candidate in ['customfield_12311140', 'customfield_10014', 'customfield_10008']:
                    if hasattr(issue.fields, candidate):
                        epic_link_field = candidate
                        break

            if epic_link_field and hasattr(issue.fields, epic_link_field):
                epic_link_data = getattr(issue.fields, epic_link_field)
                if epic_link_data:
                    # Epic link is typically just the epic key (e.g., "PROJ-123")
                    epic_link_info = str(epic_link_data)

            # Get security level information
            security_level_info = "Public (no security level)"
            if hasattr(issue.fields, 'security') and issue.fields.security:
                security_level_info = issue.fields.security.name

            # Try to find story points information
            story_points_info = None
            story_point_field = None
            for field in all_fields:
                if field.get('name', '').lower() in ['story points', 'story point estimate']:
                    story_point_field = field['id']
                    break

            # Fallback to common story point field IDs if not found by name
            if not story_point_field:
                for candidate in ['customfield_10016', 'customfield_10026', 'customfield_10004']:
                    if hasattr(issue.fields, candidate):
                        story_point_field = candidate
                        break

            if story_point_field and hasattr(issue.fields, story_point_field):
                story_points_data = getattr(issue.fields, story_point_field)
                if story_points_data is not None:
                    story_points_info = story_points_data

            issue_data = {
                "key": issue.key,
                "summary": issue.fields.summary,
                "description": issue.fields.description or "No description",
                "status": issue.fields.status.name,
                "priority": issue.fields.priority.name if issue.fields.priority else "None",
                "assignee": issue.fields.assignee.displayName if issue.fields.assignee else "Unassigned",
                "reporter": issue.fields.reporter.displayName if issue.fields.reporter else "Unknown",
                "created": str(issue.fields.created),
                "updated": str(issue.fields.updated),
                "project": issue.fields.project.name,
                "issue_type": issue.fields.issuetype.name,
                "sprint": sprint_info,
                "epic_link": epic_link_info,
                "security_level": security_level_info,
                "story_points": story_points_info,
                "url": f"{self.jira_client.server_url}/browse/{issue.key}"
            }

            # Build story points line - only show if set
            story_points_line = ""
            if issue_data['story_points'] is not None:
                story_points_line = f"**Story Points:** {issue_data['story_points']}\n"

            text = (f"**Issue: {issue_data['key']}**\n\n"
                   f"**Summary:** {issue_data['summary']}\n"
                   f"**Status:** {issue_data['status']}\n"
                   f"**Priority:** {issue_data['priority']}\n"
                   f"**Assignee:** {issue_data['assignee']}\n"
                   f"**Reporter:** {issue_data['reporter']}\n"
                   f"**Type:** {issue_data['issue_type']}\n"
                   f"**Project:** {issue_data['project']}\n"
                   f"**Sprint:** {issue_data['sprint']}\n"
                   f"**Epic Link:** {issue_data['epic_link']}\n"
                   f"{story_points_line}"
                   f"**Security Level:** {issue_data['security_level']}\n"
                   f"**Created:** {issue_data['created']}\n"
                   f"**Updated:** {issue_data['updated']}\n"
                   f"**URL:** {issue_data['url']}\n\n"
                   f"**Description:**\n{issue_data['description']}")

            return [TextContent(type="text", text=text)]

        except Exception as e:
            return [TextContent(type="text", text=f"Error fetching issue {issue_key}: {str(e)}")]

    async def _search_issues(self, jql: str, max_results: int = 50) -> List[TextContent]:
        """Search for issues using JQL"""
        try:
            issues = self.jira_client.search_issues(jql, maxResults=max_results)
            
            if not issues:
                return [TextContent(type="text", text="No issues found matching the query.")]
            
            result_text = f"**Found {len(issues)} issue(s):**\n\n"
            
            for issue in issues:
                result_text += (
                    f"• **{issue.key}** - {issue.fields.summary}\n"
                    f"  Status: {issue.fields.status.name} | "
                    f"Assignee: {issue.fields.assignee.displayName if issue.fields.assignee else 'Unassigned'}\n"
                    f"  URL: {self.jira_client.server_url}/browse/{issue.key}\n\n"
                )
            
            return [TextContent(type="text", text=result_text)]
            
        except Exception as e:
            return [TextContent(type="text", text=f"Error searching issues: {str(e)}")]

    async def _create_issue(self, project_key: str, issue_type: str, summary: str,
                          description: str, priority: str = "Normal", due_date: str = None,
                          epic_name: str = None) -> List[TextContent]:
        """Create a new Jira issue"""
        try:
            issue_dict = {
                'project': {'key': project_key},
                'summary': summary,
                'description': description,
                'issuetype': {'name': issue_type},
            }

            # Add priority if specified
            if priority:
                issue_dict['priority'] = {'name': priority}

            # Add due date if specified
            if due_date:
                issue_dict['duedate'] = due_date

            # Handle Epic Name for Epic issue types
            if issue_type.lower() == 'epic':
                # Find the Epic Name custom field
                all_fields = self.jira_client.fields()
                epic_name_field = None
                for field in all_fields:
                    if field.get('name', '').lower() == 'epic name':
                        epic_name_field = field['id']
                        break

                # Fallback to common epic name field IDs
                if not epic_name_field:
                    for candidate in ['customfield_12311141', 'customfield_10011', 'customfield_10004']:
                        # We can't easily check if the field exists without trying, so just use the first candidate
                        epic_name_field = candidate
                        break

                # Use provided epic_name or fall back to summary
                epic_name_value = epic_name if epic_name else summary

                if epic_name_field:
                    issue_dict[epic_name_field] = epic_name_value

            new_issue = self.jira_client.create_issue(fields=issue_dict)

            due_date_text = f"\n**Due Date:** {due_date}" if due_date else ""
            epic_name_text = f"\n**Epic Name:** {epic_name}" if issue_type.lower() == 'epic' and epic_name else ""
            text = (f"**Issue created successfully!**\n\n"
                   f"**Key:** {new_issue.key}\n"
                   f"**Summary:** {summary}\n"
                   f"**Type:** {issue_type}{epic_name_text}\n"
                   f"**Priority:** {priority}{due_date_text}\n"
                   f"**URL:** {self.jira_client.server_url}/browse/{new_issue.key}")

            return [TextContent(type="text", text=text)]

        except Exception as e:
            return [TextContent(type="text", text=f"Error creating issue: {str(e)}")]

    async def _update_issue(self, issue_key: str, summary: Optional[str] = None,
                          description: Optional[str] = None, story_points: Optional[float] = None,
                          priority: Optional[str] = None, assignee: Optional[str] = None,
                          security_level: Optional[str] = None) -> List[TextContent]:
        """Update an existing issue"""
        try:
            issue = self.jira_client.issue(issue_key)
            update_dict = {}

            if summary:
                update_dict['summary'] = summary
            if description:
                update_dict['description'] = description
            if priority:
                update_dict['priority'] = {'name': priority}

            # Handle assignee
            if assignee is not None:
                # Handle special values for current user
                if assignee.lower() in ['me', 'myself']:
                    # Get current user's account ID
                    current_user = self.jira_client.current_user()
                    update_dict['assignee'] = {'name': current_user}
                elif assignee == '':
                    # Empty string means unassign
                    update_dict['assignee'] = None
                elif '@' in assignee:
                    # Treat as email - need to search for user by email
                    try:
                        users = self.jira_client.search_users(assignee)
                        if users:
                            user = users[0]
                            # Try to get accountId first, fall back to name
                            if hasattr(user, 'accountId'):
                                update_dict['assignee'] = {'accountId': user.accountId}
                            elif hasattr(user, 'name'):
                                update_dict['assignee'] = {'name': user.name}
                            else:
                                # Last resort - try to use the key attribute
                                update_dict['assignee'] = {'name': user.key if hasattr(user, 'key') else str(user)}
                        else:
                            return [TextContent(type="text", text=f"Error: User with email '{assignee}' not found")]
                    except Exception as e:
                        return [TextContent(type="text", text=f"Error searching for user: {str(e)}")]
                else:
                    # Assume it's an account ID or username
                    # Try accountId first, if that fails the error will indicate to use name
                    try:
                        update_dict['assignee'] = {'accountId': assignee}
                    except:
                        update_dict['assignee'] = {'name': assignee}

            # Handle story points - need to find the custom field ID
            if story_points is not None:
                # Try common story point field names
                story_point_field = None
                all_fields = self.jira_client.fields()
                for field in all_fields:
                    if field.get('name', '').lower() in ['story points', 'story point estimate']:
                        story_point_field = field['id']
                        break

                # Fallback to common custom field IDs if not found by name
                if not story_point_field:
                    # Try the most common custom field IDs for story points
                    for candidate in ['customfield_10016', 'customfield_10026', 'customfield_10004']:
                        if hasattr(issue.fields, candidate):
                            story_point_field = candidate
                            break

                if story_point_field:
                    update_dict[story_point_field] = story_points
                else:
                    return [TextContent(type="text", text=f"Error: Could not find story points field for issue {issue_key}")]

            # Handle security level
            if security_level is not None:
                if security_level == '':
                    # Empty string means remove security level
                    update_dict['security'] = None
                else:
                    # Try to find the security level by name or use it as ID directly
                    try:
                        # Get issue metadata which includes security levels
                        issue_meta = self.jira_client._get_json(f'issue/{issue_key}/editmeta')

                        security_levels = []
                        if 'fields' in issue_meta and 'security' in issue_meta['fields']:
                            allowed_values = issue_meta['fields']['security'].get('allowedValues', [])
                            security_levels = allowed_values

                        # Try to find by name first
                        security_level_id = None
                        for level in security_levels:
                            if level.get('name') == security_level or str(level.get('id')) == security_level:
                                security_level_id = str(level.get('id'))
                                break

                        if security_level_id:
                            # Set the security level using the ID
                            update_dict['security'] = {'id': security_level_id}
                        else:
                            # If not found, list available levels
                            if security_levels:
                                available_levels = [f"{level.get('name')} (ID: {level.get('id')})" for level in security_levels]
                                return [TextContent(type="text",
                                       text=f"Error: Security level '{security_level}' not found.\nAvailable levels:\n" + "\n".join(available_levels))]
                            else:
                                return [TextContent(type="text",
                                       text=f"Error: No security levels available for this issue or project")]
                    except Exception as e:
                        return [TextContent(type="text", text=f"Error processing security level: {str(e)}")]

            if not update_dict:
                return [TextContent(type="text", text="No fields specified for update.")]

            issue.update(fields=update_dict)

            updates = []
            if summary:
                updates.append(f"Summary: {summary}")
            if description:
                updates.append("Description updated")
            if priority:
                updates.append(f"Priority: {priority}")
            if story_points is not None:
                updates.append(f"Story points: {story_points}")
            if assignee is not None:
                if assignee.lower() in ['me', 'myself']:
                    updates.append("Assignee: current user")
                elif assignee == '':
                    updates.append("Assignee: unassigned")
                else:
                    updates.append(f"Assignee: {assignee}")
            if security_level is not None:
                if security_level == '':
                    updates.append("Security level: removed")
                else:
                    updates.append(f"Security level: {security_level}")

            text = (f"**Issue {issue_key} updated successfully!**\n\n"
                   f"**Updated fields:** {', '.join(updates)}\n"
                   f"**URL:** {self.jira_client.server_url}/browse/{issue_key}")

            return [TextContent(type="text", text=text)]

        except Exception as e:
            return [TextContent(type="text", text=f"Error updating issue {issue_key}: {str(e)}")]

    async def _add_comment(self, issue_key: str, comment: str, security_level: Optional[str] = None) -> List[TextContent]:
        """Add a comment to an issue"""
        try:
            # Prepare comment data
            comment_data = {"body": comment}

            # Add security level if specified
            if security_level:
                # Try to find the security level by name or use it as ID directly
                try:
                    # Get issue metadata which includes security levels
                    # Use the REST API endpoint to get security levels
                    issue_meta = self.jira_client._get_json(f'issue/{issue_key}/editmeta')

                    security_levels = []
                    if 'fields' in issue_meta and 'security' in issue_meta['fields']:
                        allowed_values = issue_meta['fields']['security'].get('allowedValues', [])
                        security_levels = allowed_values

                    # Try to find by name first
                    security_level_id = None
                    for level in security_levels:
                        if level.get('name') == security_level or str(level.get('id')) == security_level:
                            security_level_id = str(level.get('id'))
                            break

                    if security_level_id:
                        # Add visibility to the comment data using the security level name
                        comment_data["visibility"] = {
                            "type": "group",
                            "value": security_level
                        }
                    else:
                        # If not found, list available levels
                        if security_levels:
                            available_levels = [f"{level.get('name')} (ID: {level.get('id')})" for level in security_levels]
                            return [TextContent(type="text",
                                   text=f"Error: Security level '{security_level}' not found.\nAvailable levels:\n" + "\n".join(available_levels))]
                        else:
                            return [TextContent(type="text",
                                   text=f"Error: No security levels available for this issue or project")]
                except Exception as e:
                    return [TextContent(type="text", text=f"Error processing security level: {str(e)}")]

            # Add the comment using the REST API directly
            url = f'issue/{issue_key}/comment'
            self.jira_client._session.post(
                self.jira_client._get_url(url),
                json=comment_data
            )

            security_text = f"\n**Security Level:** {security_level}" if security_level else ""
            text = (f"**Comment added to {issue_key} successfully!**\n\n"
                   f"**Comment:** {comment}{security_text}\n"
                   f"**URL:** {self.jira_client.server_url}/browse/{issue_key}")

            return [TextContent(type="text", text=text)]

        except Exception as e:
            return [TextContent(type="text", text=f"Error adding comment to {issue_key}: {str(e)}")]

    async def _get_comments(self, issue_key: str) -> List[TextContent]:
        """Get all comments for an issue"""
        try:
            issue = self.jira_client.issue(issue_key)
            comments = self.jira_client.comments(issue)
            
            if not comments:
                return [TextContent(type="text", text=f"No comments found for issue {issue_key}.")]
            
            result_text = f"**Comments for {issue_key}:**\n\n"
            
            for comment in comments:
                result_text += (
                    f"**{comment.author.displayName}** - {comment.created}\n"
                    f"{comment.body}\n"
                    f"---\n\n"
                )
            
            return [TextContent(type="text", text=result_text)]
            
        except Exception as e:
            return [TextContent(type="text", text=f"Error fetching comments for {issue_key}: {str(e)}")]

    async def _transition_issue(self, issue_key: str, transition_name: str) -> List[TextContent]:
        """Transition an issue to a new status"""
        try:
            issue = self.jira_client.issue(issue_key)
            transitions = self.jira_client.transitions(issue)
            
            # Find the transition by name
            transition_id = None
            available_transitions = []
            
            for transition in transitions:
                available_transitions.append(transition['name'])
                if transition['name'].lower() == transition_name.lower():
                    transition_id = transition['id']
                    break
            
            if not transition_id:
                text = (f"Transition '{transition_name}' not found for issue {issue_key}.\n\n"
                       f"Available transitions: {', '.join(available_transitions)}")
                return [TextContent(type="text", text=text)]
            
            self.jira_client.transition_issue(issue, transition_id)
            
            # Get updated issue to show new status
            updated_issue = self.jira_client.issue(issue_key)
            
            text = (f"**Issue {issue_key} transitioned successfully!**\n\n"
                   f"**New Status:** {updated_issue.fields.status.name}\n"
                   f"**URL:** {self.jira_client.server_url}/browse/{issue_key}")
            
            return [TextContent(type="text", text=text)]
            
        except Exception as e:
            return [TextContent(type="text", text=f"Error transitioning issue {issue_key}: {str(e)}")]

    async def _get_project(self, project_key: str) -> List[TextContent]:
        """Get information about a project"""
        try:
            project = self.jira_client.project(project_key)
            
            project_data = {
                "key": project.key,
                "name": project.name,
                "description": getattr(project, 'description', 'No description'),
                "lead": project.lead.displayName if hasattr(project, 'lead') and project.lead else "No lead",
                "project_type": getattr(project, 'projectTypeKey', 'Unknown'),
                "url": f"{self.jira_client.server_url}/projects/{project.key}"
            }
            
            text = (f"**Project: {project_data['key']}**\n\n"
                   f"**Name:** {project_data['name']}\n"
                   f"**Lead:** {project_data['lead']}\n"
                   f"**Type:** {project_data['project_type']}\n"
                   f"**URL:** {project_data['url']}\n\n"
                   f"**Description:**\n{project_data['description']}")
            
            return [TextContent(type="text", text=text)]
            
        except Exception as e:
            return [TextContent(type="text", text=f"Error fetching project {project_key}: {str(e)}")]

    async def _get_issue_types(self, project_key: str) -> List[TextContent]:
        """Get available issue types for a project"""
        try:
            project = self.jira_client.project(project_key)
            issue_types = project.issueTypes
            
            if not issue_types:
                return [TextContent(type="text", text=f"No issue types found for project {project_key}.")]
            
            result_text = f"**Issue types for project {project_key}:**\n\n"
            
            for issue_type in issue_types:
                result_text += f"• **{issue_type.name}**"
                if hasattr(issue_type, 'description') and issue_type.description:
                    result_text += f" - {issue_type.description}"
                result_text += "\n"
            
            return [TextContent(type="text", text=result_text)]
            
        except Exception as e:
            return [TextContent(type="text", text=f"Error fetching issue types for {project_key}: {str(e)}")]

    async def _get_my_issues(self, max_results: int = 20) -> List[TextContent]:
        """Get issues assigned to the current user"""
        try:
            jql = "assignee = currentUser() ORDER BY updated DESC"
            issues = self.jira_client.search_issues(jql, maxResults=max_results)
            
            if not issues:
                return [TextContent(type="text", text="No issues assigned to you found.")]
            
            result_text = f"**Your assigned issues ({len(issues)}):**\n\n"
            
            for issue in issues:
                result_text += (
                    f"• **{issue.key}** - {issue.fields.summary}\n"
                    f"  Status: {issue.fields.status.name} | "
                    f"Priority: {issue.fields.priority.name if issue.fields.priority else 'None'}\n"
                    f"  URL: {self.jira_client.server_url}/browse/{issue.key}\n\n"
                )
            
            return [TextContent(type="text", text=result_text)]
            
        except Exception as e:
            return [TextContent(type="text", text=f"Error fetching your issues: {str(e)}")]

    async def _get_project_issues(self, project_key: str, max_results: int = 50) -> List[TextContent]:
        """Get all issues for a specific project"""
        try:
            jql = f"project = {project_key} ORDER BY updated DESC"
            issues = self.jira_client.search_issues(jql, maxResults=max_results)

            if not issues:
                return [TextContent(type="text", text=f"No issues found for project {project_key}.")]

            result_text = f"**Issues in project {project_key} ({len(issues)}):**\n\n"

            for issue in issues:
                result_text += (
                    f"• **{issue.key}** - {issue.fields.summary}\n"
                    f"  Status: {issue.fields.status.name} | "
                    f"Assignee: {issue.fields.assignee.displayName if issue.fields.assignee else 'Unassigned'}\n"
                    f"  URL: {self.jira_client.server_url}/browse/{issue.key}\n\n"
                )

            return [TextContent(type="text", text=result_text)]

        except Exception as e:
            return [TextContent(type="text", text=f"Error fetching project issues: {str(e)}")]

    def _get_all_boards(self) -> list:
        """Fetch all boards, handling pagination."""
        all_boards = []
        start_at = 0
        max_results = 50
        while True:
            batch = self.jira_client.boards(startAt=start_at, maxResults=max_results)
            if not batch:
                break
            all_boards.extend(batch)
            if len(batch) < max_results:
                break
            start_at += len(batch)
        return all_boards

    def _get_all_sprints(self, board_id: int, state: Optional[str] = None) -> list:
        """Fetch all sprints from a board, handling pagination."""
        all_sprints = []
        start_at = 0
        max_results = 50
        while True:
            batch = self.jira_client.sprints(board_id, startAt=start_at, maxResults=max_results, state=state)
            if not batch:
                break
            all_sprints.extend(batch)
            if len(batch) < max_results:
                break
            start_at += len(batch)
        return all_sprints

    async def _set_sprint(self, issue_key: str, sprint_option: str,
                         sprint_value: Optional[str] = None, board_id: Optional[int] = None) -> List[TextContent]:
        """Set the sprint for a Jira issue"""
        try:
            issue = self.jira_client.issue(issue_key)

            # Find the sprint field
            all_fields = self.jira_client.fields()
            sprint_field = None
            for field in all_fields:
                if field.get('name', '').lower() == 'sprint':
                    sprint_field = field['id']
                    break

            # Fallback to common sprint field IDs
            if not sprint_field:
                for candidate in ['customfield_12310940', 'customfield_10020', 'customfield_10010']:
                    if hasattr(issue.fields, candidate):
                        sprint_field = candidate
                        break

            if not sprint_field:
                return [TextContent(type="text", text=f"Error: Could not find sprint field for issue {issue_key}")]

            # Handle removing sprint
            if sprint_option == "none":
                try:
                    # Set sprint field to None/empty
                    issue.update(fields={sprint_field: None})

                    text = (f"**Sprint removed successfully from {issue_key}!**\n\n"
                           f"**URL:** {self.jira_client.server_url}/browse/{issue_key}")

                    return [TextContent(type="text", text=text)]

                except Exception as e:
                    return [TextContent(type="text", text=f"Error removing sprint: {str(e)}")]

            # Get the board ID if not provided
            if not board_id:
                # Try to find the board from the issue's project
                try:
                    boards = self.jira_client.boards(projectKeyOrId=issue.fields.project.key)
                    if boards:
                        board_id = boards[0].id
                    else:
                        return [TextContent(type="text", text=f"Error: Could not find board for project {issue.fields.project.key}")]
                except Exception as e:
                    return [TextContent(type="text", text=f"Error finding board: {str(e)}")]

            # Get sprints from the board (paginated)
            try:
                sprints = self._get_all_sprints(board_id)
            except Exception as e:
                return [TextContent(type="text", text=f"Error fetching sprints: {str(e)}")]

            if not sprints:
                return [TextContent(type="text", text=f"Error: No sprints found for board {board_id}")]

            # Select the appropriate sprint based on the option
            selected_sprint = None

            if sprint_option == "current":
                # Find the active sprint
                for sprint in sprints:
                    if sprint.state == 'active':
                        selected_sprint = sprint
                        break
                if not selected_sprint:
                    return [TextContent(type="text", text="Error: No active sprint found")]

            elif sprint_option == "next":
                # Find the next future sprint
                future_sprints = [s for s in sprints if s.state == 'future']
                if future_sprints:
                    # Sort by start date or ID and get the first one
                    future_sprints.sort(key=lambda s: s.id)
                    selected_sprint = future_sprints[0]
                else:
                    return [TextContent(type="text", text="Error: No future sprint found")]

            elif sprint_option == "specific":
                if not sprint_value:
                    return [TextContent(type="text", text="Error: sprint_value is required when sprint_option is 'specific'")]

                # Try to find sprint by name or ID
                for sprint in sprints:
                    if (sprint.name == sprint_value or
                        str(sprint.id) == sprint_value):
                        selected_sprint = sprint
                        break

                if not selected_sprint:
                    available_sprints = [f"{s.name} (ID: {s.id})" for s in sprints]
                    return [TextContent(type="text",
                           text=f"Error: Sprint '{sprint_value}' not found.\nAvailable sprints:\n" + "\n".join(available_sprints))]

            if not selected_sprint:
                return [TextContent(type="text", text="Error: Could not determine sprint to set")]

            # Set the sprint using the Jira Python module
            try:
                self.jira_client.add_issues_to_sprint(selected_sprint.id, [issue_key])

                text = (f"**Sprint set successfully for {issue_key}!**\n\n"
                       f"**Sprint:** {selected_sprint.name}\n"
                       f"**Sprint ID:** {selected_sprint.id}\n"
                       f"**Sprint State:** {selected_sprint.state}\n"
                       f"**URL:** {self.jira_client.server_url}/browse/{issue_key}")

                return [TextContent(type="text", text=text)]

            except Exception as e:
                return [TextContent(type="text", text=f"Error setting sprint: {str(e)}")]

        except Exception as e:
            return [TextContent(type="text", text=f"Error setting sprint for {issue_key}: {str(e)}")]

    async def _set_epic_link(self, issue_key: str, epic_key: Optional[str] = None) -> List[TextContent]:
        """Set or remove the epic link for a Jira issue"""
        try:
            issue = self.jira_client.issue(issue_key)

            # Find the Epic Link custom field
            all_fields = self.jira_client.fields()
            epic_link_field = None
            for field in all_fields:
                if field.get('name', '').lower() == 'epic link':
                    epic_link_field = field['id']
                    break

            # Fallback to common epic link field IDs
            if not epic_link_field:
                for candidate in ['customfield_12311140', 'customfield_10014', 'customfield_10008']:
                    # Try to find if this field exists in the issue
                    if hasattr(issue.fields, candidate):
                        epic_link_field = candidate
                        break

            if not epic_link_field:
                return [TextContent(type="text", text=f"Error: Could not find Epic Link field for issue {issue_key}")]

            # Handle removing epic link
            if not epic_key or epic_key == "":
                try:
                    issue.update(fields={epic_link_field: None})

                    text = (f"**Epic link removed successfully from {issue_key}!**\n\n"
                           f"**URL:** {self.jira_client.server_url}/browse/{issue_key}")

                    return [TextContent(type="text", text=text)]

                except Exception as e:
                    return [TextContent(type="text", text=f"Error removing epic link: {str(e)}")]

            # Verify the epic exists
            try:
                epic = self.jira_client.issue(epic_key)
                if epic.fields.issuetype.name.lower() != 'epic':
                    return [TextContent(type="text", text=f"Error: {epic_key} is not an Epic (type: {epic.fields.issuetype.name})")]
            except Exception as e:
                return [TextContent(type="text", text=f"Error: Could not find epic {epic_key}: {str(e)}")]

            # Set the epic link
            try:
                issue.update(fields={epic_link_field: epic_key})

                text = (f"**Epic link set successfully for {issue_key}!**\n\n"
                       f"**Epic:** {epic_key} - {epic.fields.summary}\n"
                       f"**Issue URL:** {self.jira_client.server_url}/browse/{issue_key}\n"
                       f"**Epic URL:** {self.jira_client.server_url}/browse/{epic_key}")

                return [TextContent(type="text", text=text)]

            except Exception as e:
                return [TextContent(type="text", text=f"Error setting epic link: {str(e)}")]

        except Exception as e:
            return [TextContent(type="text", text=f"Error setting epic link for {issue_key}: {str(e)}")]

    async def _get_components(self, project_key: str) -> List[TextContent]:
        """Get available components for a project"""
        try:
            project = self.jira_client.project(project_key)
            components = self.jira_client.project_components(project)

            if not components:
                return [TextContent(type="text", text=f"No components found for project {project_key}.")]

            result_text = f"**Components for project {project_key}:**\n\n"

            for component in components:
                result_text += f"• **{component.name}**"
                if hasattr(component, 'description') and component.description:
                    result_text += f" - {component.description}"
                result_text += "\n"

            return [TextContent(type="text", text=result_text)]

        except Exception as e:
            return [TextContent(type="text", text=f"Error fetching components for {project_key}: {str(e)}")]

    async def _set_components(self, issue_key: str, components: List[str]) -> List[TextContent]:
        """Set components for a Jira issue"""
        try:
            issue = self.jira_client.issue(issue_key)
            project = issue.fields.project

            # Get available components for validation
            available_components = self.jira_client.project_components(project)
            available_component_names = {comp.name: comp for comp in available_components}

            # Validate that all requested components exist
            invalid_components = []
            valid_components = []

            for comp_name in components:
                if comp_name in available_component_names:
                    valid_components.append({'name': comp_name})
                else:
                    invalid_components.append(comp_name)

            if invalid_components:
                available_list = ", ".join(available_component_names.keys())
                return [TextContent(type="text",
                       text=f"Error: Invalid component(s): {', '.join(invalid_components)}\n\n"
                            f"Available components: {available_list}")]

            # Update the issue with the new components
            issue.update(fields={'components': valid_components})

            if components:
                comp_list = ", ".join(components)
                text = (f"**Components set successfully for {issue_key}!**\n\n"
                       f"**Components:** {comp_list}\n"
                       f"**URL:** {self.jira_client.server_url}/browse/{issue_key}")
            else:
                text = (f"**All components removed from {issue_key}!**\n\n"
                       f"**URL:** {self.jira_client.server_url}/browse/{issue_key}")

            return [TextContent(type="text", text=text)]

        except Exception as e:
            return [TextContent(type="text", text=f"Error setting components for {issue_key}: {str(e)}")]

    async def _get_issue_sprint_history(self, issue_key: str) -> List[TextContent]:
        """Get the history of sprint changes for an issue"""
        try:
            if not self.jira_client:
                return [TextContent(type="text", text="Jira client not initialized")]

            # Fetch issue with changelog expanded
            issue = self.jira_client.issue(issue_key, expand='changelog')

            sprint_changes = []

            # Iterate through changelog histories
            if hasattr(issue, 'changelog') and hasattr(issue.changelog, 'histories'):
                for history in issue.changelog.histories:
                    created = history.created
                    author = 'Unknown'
                if hasattr(history, 'author') and history.author is not None:
                    author = getattr(history.author, 'displayName', 'Unknown')

                    for item in history.items:
                        if item.field == 'Sprint':
                            from_sprint = item.fromString if item.fromString else None
                            to_sprint = item.toString if item.toString else None

                            sprint_changes.append({
                                'timestamp': created,
                                'author': author,
                                'from_sprint': from_sprint,
                                'to_sprint': to_sprint
                            })

            if not sprint_changes:
                return [TextContent(type="text", text=f"**Sprint History for {issue_key}**\n\nNo sprint changes found in the issue history.")]

            # Format output
            result_text = f"**Sprint History for {issue_key}**\n\n"

            for i, change in enumerate(sprint_changes, 1):
                timestamp = change['timestamp']
                author = change['author']
                from_sprint = change['from_sprint']
                to_sprint = change['to_sprint']

                if from_sprint and to_sprint:
                    action = f"Moved from \"{from_sprint}\" to \"{to_sprint}\""
                elif to_sprint:
                    action = f"Added to \"{to_sprint}\""
                elif from_sprint:
                    action = f"Removed from \"{from_sprint}\""
                else:
                    action = "Sprint changed (unknown)"

                result_text += f"{i}. **{timestamp}** - {action}\n   By: {author}\n\n"

            return [TextContent(type="text", text=result_text)]

        except Exception as e:
            return [TextContent(type="text", text=f"Error fetching sprint history for {issue_key}: {str(e)}")]

    @staticmethod
    def _categorize_status(status_name: str) -> str:
        """Categorize a status name into active, done, or backlog"""
        lower = status_name.lower()
        if lower in DONE_STATUSES:
            return 'done'
        if lower in ACTIVE_STATUSES:
            return 'active'
        if lower in BACKLOG_STATUSES:
            return 'backlog'
        # Default: treat unknown statuses as active if they're not clearly backlog
        return 'active'

    @staticmethod
    def _extract_status_transitions(issue) -> list:
        """Extract status transitions from an issue's changelog.

        Returns a list of dicts with keys: timestamp, from_status, to_status, author
        """
        transitions = []
        if hasattr(issue, 'changelog') and hasattr(issue.changelog, 'histories'):
            for history in issue.changelog.histories:
                created = history.created
                author = 'Unknown'
                if hasattr(history, 'author') and history.author is not None:
                    author = getattr(history.author, 'displayName', 'Unknown')
                for item in history.items:
                    if item.field == 'status':
                        transitions.append({
                            'timestamp': created,
                            'from_status': item.fromString or '',
                            'to_status': item.toString or '',
                            'author': author
                        })
        return transitions

    @staticmethod
    def _parse_jira_timestamp(ts: str) -> datetime:
        """Parse a Jira timestamp string into a datetime object"""
        # Jira timestamps look like: 2026-01-15T10:30:00.000+0000
        # or 2026-01-15T10:30:00.000+00:00
        # Strip milliseconds and timezone for simplicity
        ts_clean = ts.replace('T', ' ')
        # Handle timezone offset
        for sep in ['+', '-']:
            # Find the timezone part (last + or - that's not in the date)
            parts = ts_clean.rsplit(sep, 1)
            if len(parts) == 2 and ':' in parts[1] and len(parts[1]) <= 6:
                ts_clean = parts[0]
                break
        # Remove milliseconds
        if '.' in ts_clean:
            ts_clean = ts_clean.split('.')[0]
        return datetime.strptime(ts_clean, '%Y-%m-%d %H:%M:%S')

    @staticmethod
    def _count_business_days(start: datetime, end: datetime) -> float:
        """Count business days between two datetimes"""
        if end <= start:
            return 0.0
        # Count full days
        current = start.date()
        end_date = end.date()
        business_days = 0
        while current <= end_date:
            if current.weekday() < 5:  # Monday=0 to Friday=4
                business_days += 1
            current += timedelta(days=1)
        # Subtract partial first and last day
        # If start and end are same day, return fraction of that day
        if start.date() == end.date():
            hours = (end - start).total_seconds() / 3600
            return round(hours / 24, 1) if start.weekday() < 5 else 0.0
        return float(business_days)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format seconds into a human-readable duration string"""
        if seconds < 0:
            return "0h"
        total_hours = seconds / 3600
        if total_hours < 24:
            return f"{total_hours:.1f}h"
        days = int(total_hours // 24)
        hours = int(total_hours % 24)
        if hours == 0:
            return f"{days}d"
        return f"{days}d {hours}h"

    def _calculate_cycle_time(self, transitions: list, created_date: str) -> dict:
        """Calculate cycle time from status transitions.

        Returns a dict with:
        - cycle_time_seconds: total elapsed seconds from first active to last done
        - calendar_days: cycle time in calendar days
        - business_days: cycle time in business days (weekdays only)
        - first_active: timestamp of first move to active status
        - last_done: timestamp of last move to done status
        - time_in_status: dict mapping status names to seconds spent in each
        - complete: whether the issue reached a done status
        """
        if not transitions:
            return {'complete': False, 'cycle_time_seconds': 0}

        first_active_ts = None
        last_done_ts = None

        # Find first transition to an active status
        for t in transitions:
            cat = self._categorize_status(t['to_status'])
            if cat == 'active':
                first_active_ts = t['timestamp']
                break

        # Find last transition to a done status
        for t in reversed(transitions):
            cat = self._categorize_status(t['to_status'])
            if cat == 'done':
                last_done_ts = t['timestamp']
                break

        # Fallback: if no active transition found, use creation date
        if not first_active_ts and last_done_ts:
            first_active_ts = created_date

        if not first_active_ts or not last_done_ts:
            return {'complete': False, 'cycle_time_seconds': 0}

        start_dt = self._parse_jira_timestamp(first_active_ts)
        end_dt = self._parse_jira_timestamp(last_done_ts)

        if end_dt <= start_dt:
            return {'complete': False, 'cycle_time_seconds': 0}

        cycle_seconds = (end_dt - start_dt).total_seconds()
        calendar_days = round(cycle_seconds / 86400, 1)
        business_days = self._count_business_days(start_dt, end_dt)

        # Calculate time in each status
        time_in_status = {}
        # Build timeline: start from first_active through all transitions
        prev_status = None
        prev_ts = None
        for t in transitions:
            t_dt = self._parse_jira_timestamp(t['timestamp'])
            if t_dt < start_dt:
                prev_status = t['to_status']
                prev_ts = t_dt
                continue
            if prev_status and prev_ts:
                elapsed = (t_dt - max(prev_ts, start_dt)).total_seconds()
                if elapsed > 0:
                    time_in_status[prev_status] = time_in_status.get(prev_status, 0) + elapsed
            prev_status = t['to_status']
            prev_ts = t_dt
            if t_dt >= end_dt:
                break

        return {
            'complete': True,
            'cycle_time_seconds': cycle_seconds,
            'calendar_days': calendar_days,
            'business_days': business_days,
            'first_active': first_active_ts,
            'last_done': last_done_ts,
            'time_in_status': time_in_status,
        }

    async def _analyze_sprint_scope(self, sprint_name: str, board_id: Optional[int] = None) -> List[TextContent]:
        """Analyze a sprint using the sprint report API to identify planned vs added issues, punted issues, and calculate predictability"""
        try:
            if not self.jira_client:
                return [TextContent(type="text", text="Jira client not initialized")]

            # Find the sprint by name
            target_sprint = None

            if board_id:
                # Use provided board ID (paginated)
                try:
                    sprints = self._get_all_sprints(board_id)
                    for sprint in sprints:
                        if sprint.name == sprint_name:
                            target_sprint = sprint
                            break
                except Exception as e:
                    return [TextContent(type="text", text=f"Error fetching sprints from board {board_id}: {str(e)}")]
            else:
                # Try to find the sprint across all boards
                try:
                    boards = self._get_all_boards()
                    for board in boards:
                        try:
                            sprints = self._get_all_sprints(board.id)
                            for sprint in sprints:
                                if sprint.name == sprint_name:
                                    target_sprint = sprint
                                    board_id = board.id
                                    break
                            if target_sprint:
                                break
                        except:
                            continue
                except Exception as e:
                    return [TextContent(type="text", text=f"Error searching for sprint: {str(e)}")]

            if not target_sprint:
                return [TextContent(type="text", text=f"Error: Sprint '{sprint_name}' not found")]

            # Get sprint details
            sprint_id = target_sprint.id
            sprint_start = getattr(target_sprint, 'startDate', None)
            sprint_end = getattr(target_sprint, 'endDate', None)
            sprint_state = getattr(target_sprint, 'state', 'unknown')

            if not sprint_start:
                return [TextContent(type="text", text=f"Error: Sprint '{sprint_name}' has no start date (may not have been started yet)")]

            # Call the sprint report API
            report_url = f"{self.jira_client.server_url}/rest/greenhopper/1.0/rapid/charts/sprintreport?rapidViewId={board_id}&sprintId={sprint_id}"
            try:
                response = self.jira_client._session.get(report_url)
                response.raise_for_status()
                report = response.json()
            except Exception as e:
                return [TextContent(type="text", text=f"Error fetching sprint report: {str(e)}")]

            contents = report.get('contents', {})

            # Get the set of issue keys added during sprint
            added_keys = set(contents.get('issueKeysAddedDuringSprint', {}).keys())

            def get_sp(issue_data):
                """Extract story points from sprint report issue data"""
                stat = issue_data.get('currentEstimateStatistic', {})
                val = stat.get('statFieldValue', {})
                return val.get('value', 0) or 0

            def get_issue_info(issue_data):
                """Extract issue info from sprint report issue data"""
                key = issue_data.get('key', '')
                summary = issue_data.get('summary', '')
                if len(summary) > 60:
                    summary = summary[:57] + '...'
                sp = get_sp(issue_data)
                status = issue_data.get('status', {}).get('name', '')
                is_added = key in added_keys
                return {
                    'key': key,
                    'summary': summary,
                    'sp': sp,
                    'status': status,
                    'is_added': is_added,
                }

            # Categorize issues from the report
            completed_issues = [get_issue_info(i) for i in contents.get('completedIssues', [])]
            not_completed_issues = [get_issue_info(i) for i in contents.get('issuesNotCompletedInCurrentSprint', [])]
            punted_issues = [get_issue_info(i) for i in contents.get('puntedIssues', [])]
            completed_elsewhere = [get_issue_info(i) for i in contents.get('issuesCompletedInAnotherSprint', [])]

            # Split each category into planned vs added
            completed_planned = [i for i in completed_issues if not i['is_added']]
            completed_added = [i for i in completed_issues if i['is_added']]
            not_completed_planned = [i for i in not_completed_issues if not i['is_added']]
            not_completed_added = [i for i in not_completed_issues if i['is_added']]
            punted_planned = [i for i in punted_issues if not i['is_added']]
            punted_added = [i for i in punted_issues if i['is_added']]
            elsewhere_planned = [i for i in completed_elsewhere if not i['is_added']]
            elsewhere_added = [i for i in completed_elsewhere if i['is_added']]

            # Calculate SP totals
            completed_planned_sp = sum(i['sp'] for i in completed_planned)
            completed_added_sp = sum(i['sp'] for i in completed_added)
            not_completed_planned_sp = sum(i['sp'] for i in not_completed_planned)
            not_completed_added_sp = sum(i['sp'] for i in not_completed_added)
            punted_planned_sp = sum(i['sp'] for i in punted_planned)
            punted_added_sp = sum(i['sp'] for i in punted_added)
            elsewhere_planned_sp = sum(i['sp'] for i in elsewhere_planned)
            elsewhere_added_sp = sum(i['sp'] for i in elsewhere_added)

            total_planned_sp = completed_planned_sp + not_completed_planned_sp + punted_planned_sp + elsewhere_planned_sp
            total_added_sp = completed_added_sp + not_completed_added_sp + punted_added_sp + elsewhere_added_sp

            all_planned = completed_planned + not_completed_planned + punted_planned + elsewhere_planned
            all_added = completed_added + not_completed_added + punted_added + elsewhere_added

            # Calculate predictability
            # Include issues completed in another sprint as "done" planned work
            done_planned_sp = completed_planned_sp + elsewhere_planned_sp
            total_denominator = total_planned_sp + total_added_sp
            if total_denominator > 0:
                predictability = (done_planned_sp / total_denominator) * 100
            else:
                predictability = 0.0

            # Format output
            sprint_start_str = str(sprint_start)[:10] if sprint_start else 'Unknown'
            sprint_end_str = str(sprint_end)[:10] if sprint_end else 'Unknown'

            result_text = f"**Sprint Scope Analysis: {sprint_name}**\n\n"
            result_text += f"**Sprint Start:** {sprint_start_str}\n"
            result_text += f"**Sprint End:** {sprint_end_str}\n"
            result_text += f"**State:** {sprint_state}\n\n"

            result_text += f"**Predictability: {predictability:.1f}%**\n"
            result_text += f"Formula: Completed Planned SP ({done_planned_sp}) / (All Committed SP ({total_planned_sp}) + All Added SP ({total_added_sp}))\n\n"

            result_text += f"**Planned Issues ({len(all_planned)}):** {total_planned_sp} SP (committed at sprint start)\n"
            result_text += f"  - Completed: {completed_planned_sp} SP ({len(completed_planned)} issues)\n"
            result_text += f"  - Not Completed: {not_completed_planned_sp} SP ({len(not_completed_planned)} issues)\n"
            result_text += f"  - Punted/Removed: {punted_planned_sp} SP ({len(punted_planned)} issues)\n"
            if elsewhere_planned:
                result_text += f"  - Completed in Another Sprint: {elsewhere_planned_sp} SP ({len(elsewhere_planned)} issues)\n"
            result_text += "\n"

            result_text += f"**Added Mid-Sprint ({len(all_added)}):** {total_added_sp} SP (scope creep)\n"
            result_text += f"  - Completed: {completed_added_sp} SP ({len(completed_added)} issues)\n"
            result_text += f"  - Not Completed: {not_completed_added_sp} SP ({len(not_completed_added)} issues)\n"
            if punted_added:
                result_text += f"  - Punted/Removed: {punted_added_sp} SP ({len(punted_added)} issues)\n"
            if elsewhere_added:
                result_text += f"  - Completed in Another Sprint: {elsewhere_added_sp} SP ({len(elsewhere_added)} issues)\n"
            result_text += "\n"

            # Punted issues table
            all_punted = punted_planned + punted_added
            if all_punted:
                result_text += "**Punted Issues:**\n\n"
                result_text += "| Issue | SP | Summary |\n"
                result_text += "|-------|----|---------|\n"
                for issue_info in all_punted:
                    sp = issue_info['sp'] if issue_info['sp'] else '-'
                    result_text += f"| {issue_info['key']} | {sp} | {issue_info['summary']} |\n"
                result_text += "\n"

            # Added mid-sprint issues table
            if all_added:
                result_text += "**Added Mid-Sprint Issues:**\n\n"
                result_text += "| Issue | SP | Status | Summary |\n"
                result_text += "|-------|----|---------|---------|\n"
                for issue_info in all_added:
                    sp = issue_info['sp'] if issue_info['sp'] else '-'
                    result_text += f"| {issue_info['key']} | {sp} | {issue_info['status']} | {issue_info['summary']} |\n"
                result_text += "\n"

            return [TextContent(type="text", text=result_text)]

        except Exception as e:
            return [TextContent(type="text", text=f"Error analyzing sprint scope: {str(e)}")]

    async def _get_issue_cycle_time(self, issue_key: str) -> List[TextContent]:
        """Get cycle time and status transition timeline for a single issue"""
        try:
            if not self.jira_client:
                return [TextContent(type="text", text="Jira client not initialized")]

            # Fetch issue with changelog
            issue = self.jira_client.issue(issue_key, expand='changelog')
            created_date = str(issue.fields.created)

            transitions = self._extract_status_transitions(issue)

            if not transitions:
                return [TextContent(type="text",
                    text=f"**Cycle Time for {issue_key}**\n\n"
                         f"No status transitions found in the issue history.\n"
                         f"Current status: {issue.fields.status.name}")]

            cycle_data = self._calculate_cycle_time(transitions, created_date)

            # Format output
            result_text = f"**Cycle Time for {issue_key}**\n\n"
            result_text += f"**Summary:** {issue.fields.summary}\n"
            result_text += f"**Type:** {issue.fields.issuetype.name}\n"
            result_text += f"**Current Status:** {issue.fields.status.name}\n\n"

            if cycle_data['complete']:
                result_text += f"**Cycle Time:** {self._format_duration(cycle_data['cycle_time_seconds'])}\n"
                result_text += f"**Calendar Days:** {cycle_data['calendar_days']}\n"
                result_text += f"**Business Days:** {cycle_data['business_days']}\n"
                result_text += f"**Started:** {cycle_data['first_active']}\n"
                result_text += f"**Completed:** {cycle_data['last_done']}\n\n"

                # Time in each status
                time_in_status = cycle_data.get('time_in_status', {})
                if time_in_status:
                    result_text += "**Time in Each Status:**\n\n"
                    result_text += "| Status | Time | Category |\n"
                    result_text += "|--------|------|----------|\n"
                    # Sort by time spent descending
                    sorted_statuses = sorted(time_in_status.items(), key=lambda x: x[1], reverse=True)
                    for status_name, seconds in sorted_statuses:
                        category = self._categorize_status(status_name)
                        result_text += f"| {status_name} | {self._format_duration(seconds)} | {category} |\n"
                    result_text += "\n"
            else:
                result_text += "**Cycle Time:** Not completed (issue has not reached a done status)\n\n"

            # Full transition timeline
            result_text += "**Status Transition Timeline:**\n\n"
            for i, t in enumerate(transitions, 1):
                result_text += f"{i}. **{t['timestamp']}** - {t['from_status']} -> {t['to_status']}\n"
                result_text += f"   By: {t['author']}\n\n"

            return [TextContent(type="text", text=result_text)]

        except Exception as e:
            return [TextContent(type="text", text=f"Error fetching cycle time for {issue_key}: {str(e)}")]

    def _get_story_points(self, issue) -> float:
        """Extract story points from an issue object."""
        all_fields = self.jira_client.fields()
        story_point_field = None
        for field in all_fields:
            if field.get('name', '').lower() in ['story points', 'story point estimate']:
                story_point_field = field['id']
                break
        if not story_point_field:
            for candidate in ['customfield_10016', 'customfield_10026', 'customfield_10004']:
                if hasattr(issue.fields, candidate):
                    story_point_field = candidate
                    break
        if story_point_field and hasattr(issue.fields, story_point_field):
            sp = getattr(issue.fields, story_point_field)
            if sp is not None:
                return float(sp)
        return 0

    def _find_assigned_team_field(self) -> Optional[str]:
        """Find the custom field ID for AssignedTeam."""
        all_fields = self.jira_client.fields()
        for field in all_fields:
            field_name = field.get('name', '')
            field_clauseNames = field.get('clauseNames', [])
            if field_name == 'Assigned Team' or 'AssignedTeam' in field_clauseNames:
                return field['id']
        for field in all_fields:
            field_name_lower = field.get('name', '').lower()
            field_id_lower = field.get('id', '').lower()
            if 'assignedteam' in field_id_lower or 'assigned_team' in field_name_lower or 'assigned team' in field_name_lower:
                return field['id']
        return None

    def _fetch_issues_by_date_range(self, start_date: str, end_date: str,
                                     team: Optional[str] = None) -> list:
        """Fetch closed issues in a date range using JQL. Returns issue objects with changelog."""
        jql = f'status = Closed AND resolved >= "{start_date}" AND resolved < "{end_date}"'
        if team:
            jql += f' AND AssignedTeam = "{team}"'
        jql += ' ORDER BY resolved ASC'

        issues = []
        start_at = 0
        max_results = 50
        while True:
            batch = self.jira_client.search_issues(
                jql, startAt=start_at, maxResults=max_results, expand='changelog'
            )
            if not batch:
                break
            issues.extend(batch)
            if len(batch) < max_results:
                break
            start_at += len(batch)
        return issues

    def _fetch_issues_by_sprint(self, sprint_name: str,
                                 board_id: Optional[int] = None) -> tuple:
        """Fetch completed issues from a sprint report. Returns (issues_data, sprint_info) or raises."""
        target_sprint = None

        if board_id:
            sprints = self._get_all_sprints(board_id)
            for sprint in sprints:
                if sprint.name == sprint_name:
                    target_sprint = sprint
                    break
        else:
            boards = self._get_all_boards()
            for board in boards:
                try:
                    sprints = self._get_all_sprints(board.id)
                    for sprint in sprints:
                        if sprint.name == sprint_name:
                            target_sprint = sprint
                            board_id = board.id
                            break
                    if target_sprint:
                        break
                except:
                    continue

        if not target_sprint:
            raise ValueError(f"Sprint '{sprint_name}' not found")

        report_url = f"{self.jira_client.server_url}/rest/greenhopper/1.0/rapid/charts/sprintreport?rapidViewId={board_id}&sprintId={target_sprint.id}"
        response = self.jira_client._session.get(report_url)
        response.raise_for_status()
        report = response.json()

        contents = report.get('contents', {})
        completed_issues_data = contents.get('completedIssues', [])
        return completed_issues_data, target_sprint

    async def _analyze_cycle_time(self, start_date: Optional[str] = None,
                                   end_date: Optional[str] = None,
                                   team: Optional[str] = None,
                                   sprint_name: Optional[str] = None,
                                   board_id: Optional[int] = None) -> List[TextContent]:
        """Analyze cycle time statistics for completed issues in a date range or sprint."""
        try:
            if not self.jira_client:
                return [TextContent(type="text", text="Jira client not initialized")]

            # Validate inputs
            use_date_range = bool(start_date and end_date)
            use_sprint = bool(sprint_name)

            if not use_date_range and not use_sprint:
                return [TextContent(type="text",
                    text="Error: Provide either start_date + end_date, or sprint_name")]

            # Build title for output
            if use_date_range:
                title = f"{start_date} to {end_date}"
                if team:
                    title += f" ({team})"
            else:
                title = sprint_name

            # Collect issues and calculate cycle times
            cycle_results = []
            skipped_no_transitions = 0
            skipped_team_filter = 0
            sprint_info_text = ""

            if use_date_range:
                # Date range mode: JQL search (team filter applied in JQL)
                issues = self._fetch_issues_by_date_range(start_date, end_date, team)
                if not issues:
                    return [TextContent(type="text",
                        text=f"**Cycle Time Analysis: {title}**\n\nNo closed issues found in this date range.")]

                for issue in issues:
                    created_date = str(issue.fields.created)
                    transitions = self._extract_status_transitions(issue)
                    if not transitions:
                        skipped_no_transitions += 1
                        continue

                    cycle_data = self._calculate_cycle_time(transitions, created_date)
                    if not cycle_data['complete']:
                        skipped_no_transitions += 1
                        continue

                    sp = self._get_story_points(issue)
                    summary = str(issue.fields.summary or '')
                    if len(summary) > 50:
                        summary = summary[:47] + '...'

                    cycle_results.append({
                        'key': issue.key,
                        'summary': summary,
                        'type': issue.fields.issuetype.name,
                        'sp': sp,
                        'cycle_days': cycle_data['calendar_days'],
                        'business_days': cycle_data['business_days'],
                        'cycle_seconds': cycle_data['cycle_time_seconds'],
                        'time_in_status': cycle_data.get('time_in_status', {}),
                    })

                sprint_info_text = f"**Period:** {start_date} to {end_date}\n"

            else:
                # Sprint mode: use sprint report API
                try:
                    completed_issues_data, target_sprint = self._fetch_issues_by_sprint(
                        sprint_name, board_id)
                except ValueError as e:
                    return [TextContent(type="text", text=f"Error: {str(e)}")]
                except Exception as e:
                    return [TextContent(type="text", text=f"Error fetching sprint report: {str(e)}")]

                if not completed_issues_data:
                    return [TextContent(type="text",
                        text=f"**Cycle Time Analysis: {title}**\n\nNo completed issues found in this sprint.")]

                # Find team field for filtering
                assigned_team_field = self._find_assigned_team_field() if team else None

                for issue_data in completed_issues_data:
                    issue_key = issue_data.get('key', '')
                    if not issue_key:
                        continue

                    try:
                        issue = self.jira_client.issue(issue_key, expand='changelog')
                    except Exception:
                        continue

                    # Apply team filter
                    if team and assigned_team_field:
                        issue_team = getattr(issue.fields, assigned_team_field, None)
                        if issue_team:
                            team_name = issue_team.value if hasattr(issue_team, 'value') else str(issue_team)
                            if team_name != team:
                                skipped_team_filter += 1
                                continue
                        else:
                            skipped_team_filter += 1
                            continue

                    created_date = str(issue.fields.created)
                    transitions = self._extract_status_transitions(issue)
                    if not transitions:
                        skipped_no_transitions += 1
                        continue

                    cycle_data = self._calculate_cycle_time(transitions, created_date)
                    if not cycle_data['complete']:
                        skipped_no_transitions += 1
                        continue

                    sp = 0
                    stat = issue_data.get('currentEstimateStatistic', {})
                    val = stat.get('statFieldValue', {})
                    sp = val.get('value', 0) or 0

                    summary = issue_data.get('summary', issue.fields.summary or '')
                    if len(summary) > 50:
                        summary = summary[:47] + '...'

                    cycle_results.append({
                        'key': issue_key,
                        'summary': summary,
                        'type': issue.fields.issuetype.name,
                        'sp': sp,
                        'cycle_days': cycle_data['calendar_days'],
                        'business_days': cycle_data['business_days'],
                        'cycle_seconds': cycle_data['cycle_time_seconds'],
                        'time_in_status': cycle_data.get('time_in_status', {}),
                    })

                sprint_state = getattr(target_sprint, 'state', 'unknown')
                sprint_start = getattr(target_sprint, 'startDate', None)
                sprint_end = getattr(target_sprint, 'endDate', None)
                sprint_start_str = str(sprint_start)[:10] if sprint_start else 'Unknown'
                sprint_end_str = str(sprint_end)[:10] if sprint_end else 'Unknown'
                sprint_info_text = f"**Sprint:** {sprint_start_str} to {sprint_end_str} ({sprint_state})\n"

            if not cycle_results:
                msg = f"**Cycle Time Analysis: {title}**\n\nNo completed issues with valid cycle time data."
                if skipped_no_transitions:
                    msg += f"\n{skipped_no_transitions} issue(s) skipped (no status transitions)."
                if skipped_team_filter:
                    msg += f"\n{skipped_team_filter} issue(s) filtered out by team filter."
                return [TextContent(type="text", text=msg)]

            # Calculate statistics
            cycle_days_list = [r['cycle_days'] for r in cycle_results]
            business_days_list = [r['business_days'] for r in cycle_results]

            median_days = round(statistics.median(cycle_days_list), 1)
            avg_days = round(statistics.mean(cycle_days_list), 1)
            p85_days = round(sorted(cycle_days_list)[int(len(cycle_days_list) * 0.85)], 1) if len(cycle_days_list) > 1 else cycle_days_list[0]
            median_bdays = round(statistics.median(business_days_list), 1)

            # Sort by cycle time descending
            cycle_results.sort(key=lambda x: x['cycle_seconds'], reverse=True)

            # Identify outliers (> 2x median)
            outlier_threshold = median_days * 2
            outliers = [r for r in cycle_results if r['cycle_days'] > outlier_threshold]

            # Breakdown by issue type
            type_stats = {}
            for r in cycle_results:
                t = r['type']
                if t not in type_stats:
                    type_stats[t] = []
                type_stats[t].append(r['cycle_days'])

            # Format output
            result_text = f"**Cycle Time Analysis: {title}**\n\n"
            result_text += sprint_info_text
            result_text += f"**Completed Issues Analyzed:** {len(cycle_results)}\n"
            if team:
                result_text += f"**Team Filter:** {team}\n"
            if skipped_no_transitions:
                result_text += f"**Skipped (no transitions):** {skipped_no_transitions}\n"
            if skipped_team_filter:
                result_text += f"**Filtered out (team):** {skipped_team_filter}\n"
            result_text += "\n"

            result_text += "**Cycle Time Statistics (calendar days):**\n"
            result_text += f"  - Median: **{median_days} days**\n"
            result_text += f"  - Average: {avg_days} days\n"
            result_text += f"  - 85th Percentile: {p85_days} days\n"
            result_text += f"  - Median (business days): {median_bdays} days\n\n"

            # Breakdown by type
            if len(type_stats) > 1:
                result_text += "**Breakdown by Issue Type:**\n\n"
                result_text += "| Type | Count | Median | Average |\n"
                result_text += "|------|-------|--------|---------|\n"
                for itype, days_list in sorted(type_stats.items()):
                    count = len(days_list)
                    t_median = round(statistics.median(days_list), 1)
                    t_avg = round(statistics.mean(days_list), 1)
                    result_text += f"| {itype} | {count} | {t_median}d | {t_avg}d |\n"
                result_text += "\n"

            # Per-issue table
            result_text += "**Per-Issue Cycle Times:**\n\n"
            result_text += "| Issue | Type | SP | Cycle (cal) | Cycle (biz) | Summary |\n"
            result_text += "|-------|------|----|-------------|-------------|---------|\n"
            for r in cycle_results:
                sp = r['sp'] if r['sp'] else '-'
                flag = " **" if r['cycle_days'] > outlier_threshold else ""
                flag_end = "**" if flag else ""
                result_text += f"| {r['key']} | {r['type']} | {sp} | {flag}{r['cycle_days']}d{flag_end} | {r['business_days']}d | {r['summary']} |\n"
            result_text += "\n"

            # Outliers section
            if outliers:
                result_text += f"**Outliers (>{outlier_threshold} days, >2x median):**\n\n"
                for r in outliers:
                    result_text += f"- **{r['key']}** ({r['cycle_days']} days): {r['summary']}\n"
                    tis = r.get('time_in_status', {})
                    if tis:
                        sorted_tis = sorted(tis.items(), key=lambda x: x[1], reverse=True)
                        status_parts = [f"{name}: {self._format_duration(secs)}" for name, secs in sorted_tis[:3]]
                        result_text += f"  Top statuses: {', '.join(status_parts)}\n"
                result_text += "\n"

            return [TextContent(type="text", text=result_text)]

        except Exception as e:
            return [TextContent(type="text", text=f"Error analyzing cycle time: {str(e)}")]

    async def run(self):
        """Run the MCP server"""
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="jira-mcp-server",
                    server_version="1.0.0",
                    capabilities=ServerCapabilities(tools={}),
                ),
            )


async def main():
    """Main entry point"""
    server = JiraMCPServer()
    await server.run()


if __name__ == "__main__":
    asyncio.run(main()) 