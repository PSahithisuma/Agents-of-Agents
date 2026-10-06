from __future__ import annotations
import os
from fastapi import APIRouter, Depends
from core.auth import AuthUser, get_current_user
import httpx

router = APIRouter(prefix="/api/v1/mcp", tags=["MCP - Model Context Protocol"])

GITEA_URL = "http://gitea:3000"
GITEA_TOKEN = os.environ["GITEA_TOKEN"]

@router.get("/context", summary="Get full platform context for AI coding tools")
async def get_mcp_context(user: AuthUser = Depends(get_current_user)):
    """
    MCP endpoint — connect Cursor/Claude/Windsurf to Agents of Agents.
    Returns platform context: projects, entities, compliance, APIs.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        repos_r = await client.get(
            f"{GITEA_URL}/api/v1/repos/search?limit=10&sort=newest",
            headers={"Authorization": f"token {GITEA_TOKEN}"}
        )
        repos = repos_r.json().get("data", []) if repos_r.status_code == 200 else []

    projects = []
    for repo in repos:
        name = repo.get("name", "")
        pack = "banking"
        for p in ["banking","ecommerce","logistics","custom"]:
            if p in name:
                pack = p
                break
        projects.append({
            "repo": name,
            "pack": pack,
            "gitea_url": repo.get("html_url"),
            "created_at": repo.get("created"),
            "description": repo.get("description"),
        })

    return {
        "platform": "Agents of Agents",
        "version": "2.0",
        "oracle_cloud": "India South (Hyderabad)",
        "capabilities": [
            "Spring Boot code generation",
            "ERD diagram generation",
            "JUnit test generation",
            "Self-healing QA (3 retries)",
            "Dockerfile + docker-compose generation",
            "Flyway SQL migration generation",
        ],
        "packs": {
            "banking":   {"tables": 24, "compliance": "RBI Tier-1, PCI-DSS", "package": "com.vibebank"},
            "ecommerce": {"tables": 28, "compliance": "GDPR, GST/HSN",       "package": "com.omnistore"},
            "logistics": {"tables": 18, "compliance": "ISO 27001",            "package": "com.logiroute"},
            "custom":    {"tables": 20, "compliance": "Custom",               "package": "com.customapp"},
        },
        "files_per_generation": [
            "pom.xml", "Dockerfile", "docker-compose.yml",
            "src/main/java/.../entity/*.java",
            "src/main/java/.../repository/*.java",
            "src/main/java/.../service/*.java",
            "src/main/java/.../controller/*.java",
            "src/main/resources/application.yml",
            "src/main/resources/db/migration/V1__create_tables.sql",
            "src/test/java/.../*ServiceTest.java",
            "ERD.md"
        ],
        "recent_projects": projects[:5],
        "api_base": "http://144.24.149.226:8000",
        "gitea_base": "http://144.24.149.226:3002",
        "mcp_instructions": "Use POST /api/v1/chat/analyze to analyze requirements. Use POST /api/v1/projects/{id}/generate to generate code. Use GET /api/v1/projects/{id}/jobs/{jid}/download for ZIP."
    }

@router.get("/tools", summary="List available MCP tools")
async def list_mcp_tools(user: AuthUser = Depends(get_current_user)):
    return {
        "tools": [
            {"name": "analyze_requirement", "endpoint": "POST /api/v1/chat/analyze", "description": "AI analyzes plain English requirement → returns generation plan"},
            {"name": "preview_generation",  "endpoint": "POST /api/v1/projects/{id}/preview", "description": "Preview what will be generated before executing"},
            {"name": "generate_project",    "endpoint": "POST /api/v1/projects/{id}/generate", "description": "Generate complete Spring Boot project"},
            {"name": "download_zip",        "endpoint": "GET /api/v1/projects/{id}/jobs/{jid}/download", "description": "Download all generated files as ZIP"},
            {"name": "stream_progress",     "endpoint": "GET /api/v1/jobs/{id}/stream", "description": "SSE real-time generation progress"},
            {"name": "get_suggestions",     "endpoint": "GET /api/v1/chat/suggestions", "description": "Get example prompts for inspiration"},
        ]
    }
