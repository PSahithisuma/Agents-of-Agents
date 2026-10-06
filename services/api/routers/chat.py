from __future__ import annotations
from fastapi import APIRouter, Depends, Body
from pydantic import BaseModel
from core.auth import AuthUser, get_current_user
import httpx, os

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = "qwen/qwen3.8-27b"

class ChatMessage(BaseModel):
    message: str

@router.post("/analyze", summary="AI analyzes your requirement and returns generation plan")
async def chat_analyze(
    body: ChatMessage,
    user: AuthUser = Depends(get_current_user),
):
    """
    Send any requirement in plain English.
    AI returns a generation plan (pack, entity, files, compliance).
    """
    prompt = f"""Analyze this software requirement and return a JSON response:
Requirement: "{body.message}"

Return ONLY valid JSON with these fields:
{{
  "pack": "banking|ecommerce|logistics|custom",
  "entity_name": "SuggestedEntityName",
  "app_name": "AppName",
  "description": "Clean one-line description",
  "compliance": "Relevant compliance standard",
  "tables": 15,
  "key_entities": ["Entity1", "Entity2", "Entity3"],
  "suggested_fields": ["field1", "field2", "field3", "field4", "field5"],
  "summary": "One sentence what this app does"
}}

Rules:
- pack must be one of: banking, ecommerce, logistics, custom
- entity_name should be PascalCase Java class name
- tables should be realistic number
- compliance: RBI Tier-1 for banking, GDPR for ecommerce, ISO 27001 for logistics, HIPAA/Custom for others
- key_entities: 3-5 main entities
"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 500}
            )
            text = resp.json()["choices"][0]["message"]["content"]
            # Extract JSON from response
            import json, re
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                plan = json.loads(match.group())
                plan["ready"] = True
                plan["estimated_seconds"] = 15
                return plan
            return {"error": "Could not parse AI response", "raw": text}
    except Exception as e:
        return {"error": str(e), "pack": "custom", "entity_name": "CustomEntity", "ready": False}

@router.get("/suggestions", summary="Get example prompts for inspiration")
async def get_suggestions(user: AuthUser = Depends(get_current_user)):
    return {
        "suggestions": [
            "Build a hospital management system with patient records, doctor schedules and HIPAA compliance",
            "Create a school ERP with students, teachers, attendance, fees and grade management",
            "Build a restaurant POS system with menu, orders, kitchen display and GST billing",
            "Create a real estate platform with properties, buyers, agents and document management",
            "Build a gym management system with members, trainers, class schedules and payments",
            "Create an HR system with employees, payroll, leave management and performance reviews",
            "Build a hotel booking system with rooms, guests, reservations and housekeeping",
            "Create an inventory management system with products, warehouses, stock and suppliers"
        ]
    }
