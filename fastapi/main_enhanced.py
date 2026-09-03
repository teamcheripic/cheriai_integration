#!/usr/bin/env python3
"""
CheriPic AI Backend - Enhanced FastAPI Server
Handles CheriAI conversation, user verification, rate limiting, and conversation history
"""

import os
import logging
from datetime import datetime
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
from dotenv import load_dotenv

# Rate limiting
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv()

from supabase_client import supabase
from llm_client import send_to_llm
from cheriai_prompts import CheriAIPromptBuilder

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 8000))
FRONTEND_URLS = os.getenv("FRONTEND_URLS", "http://localhost:5176,http://localhost:5173,http://127.0.0.1:5176").split(",")

# Rate limiter
limiter = Limiter(key_func=get_remote_address)

# Request/Response Models
class ChatRequest(BaseModel):
    """Request model for chat endpoint"""
    user_id: str
    message: str
    stage: Optional[str] = "general"  # onboarding, golden_questions, matching, general
    context: Optional[dict] = None
    conversation_id: Optional[str] = None
    
    @validator('user_id')
    def user_id_not_empty(cls, v):
        if not v or len(v.strip()) == 0:
            raise ValueError("user_id cannot be empty")
        return v
    
    @validator('message')
    def message_not_empty(cls, v):
        if not v or len(v.strip()) == 0:
            raise ValueError("message cannot be empty")
        return v

class ConversationMessage(BaseModel):
    """Single message in conversation"""
    role: str  # "user" or "assistant"
    content: str
    timestamp: str

class ChatResponse(BaseModel):
    """Response model for chat endpoint"""
    reply: str
    user: Optional[dict] = None
    conversation_id: str
    stage: str
    timestamp: str

class ConversationHistoryResponse(BaseModel):
    """Response model for conversation history"""
    user_id: str
    conversation_id: str
    messages: List[ConversationMessage]
    stage: str
    created_at: str

# Dependency: Verify user exists in database
async def verify_user(user_id: str) -> dict:
    """Check if user exists in Supabase and return user data"""
    try:
        resp = supabase.table("user_profiles").select("*").eq("id", user_id).limit(1).execute()
        
        user_data = None
        if hasattr(resp, "data") and isinstance(resp.data, list) and len(resp.data) > 0:
            user_data = resp.data[0]
        elif isinstance(resp, dict) and "data" in resp and resp.get("data"):
            user_data = resp["data"][0] if isinstance(resp["data"], list) else resp["data"]
        
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found in database"
            )
        
        return user_data
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"User verification error for {user_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="User verification failed"
        )

# App lifecycle
@asynccontextmanager
async def lifespan(app: FastAPI):
    """App startup and shutdown"""
    logger.info("🚀 CheriPic AI Backend starting...")
    logger.info(f"Allowed origins: {FRONTEND_URLS}")
    yield
    logger.info("🛑 CheriPic AI Backend shutting down...")

# Create FastAPI app
app = FastAPI(
    title="CheriPic AI Backend",
    description="AI-powered relationship coach and matching support for CheriPic",
    version="1.0.0",
    lifespan=lifespan
)

# Add rate limiter to app
app.state.limiter = limiter

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[url.strip() for url in FRONTEND_URLS],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
@app.get("/", tags=["Health"])
async def root():
    """Root endpoint"""
    return {
        "service": "CheriPic AI Backend",
        "status": "running",
        "version": "1.0.0"
    }

@app.get("/health", tags=["Health"])
async def health():
    """Health check endpoint"""
    return {
        "status": "ok",
        "service": "CheriPic AI Backend",
        "timestamp": datetime.now().isoformat()
    }

@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
@limiter.limit("30/minute")
async def chat(req: ChatRequest):
    """
    Main chat endpoint for CheriAI conversations
    
    Flow:
    1. Verify user exists in database
    2. Fetch user data from Supabase
    3. Build CheriAI-specific prompt based on stage
    4. Send to LLM (OpenAI) and get response
    5. Store conversation history
    6. Return response
    
    Parameters:
    - user_id: UUID of the user
    - message: User's message
    - stage: Conversation stage (onboarding, golden_questions, matching, general)
    - context: Optional context data (interests, dealbreakers, etc.)
    - conversation_id: Optional ID to continue existing conversation
    """
    
    logger.info(f"💬 Chat request | User: {req.user_id} | Stage: {req.stage} | Message length: {len(req.message)}")
    
    try:
        # 1. Verify user
        user_data = await verify_user(req.user_id)
        logger.info(f"✅ User verified: {user_data.get('nick_name', 'Unknown')}")
        
        # 2. Build CheriAI prompt
        prompt_builder = CheriAIPromptBuilder(user_data, req.stage)
        full_prompt = prompt_builder.build(req.message, req.context)
        logger.debug(f"Prompt built for stage: {req.stage}")
        
        # 3. Call LLM
        logger.info("🤖 Calling LLM...")
        llm_reply = await send_to_llm(full_prompt, user=req.user_id)
        logger.info(f"✅ LLM response received ({len(llm_reply)} characters)")
        
        # 4. Generate or use existing conversation ID
        conversation_id = req.conversation_id or f"conv_{req.user_id}_{int(datetime.now().timestamp())}"
        
        # 5. Save conversation history (fire and forget)
        try:
            await save_conversation_message(
                req.user_id,
                conversation_id,
                "user",
                req.message,
                req.stage
            )
            await save_conversation_message(
                req.user_id,
                conversation_id,
                "assistant",
                llm_reply,
                req.stage
            )
            logger.info(f"💾 Conversation saved | ID: {conversation_id}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to save conversation history: {e}")
            # Don't fail request if history save fails
        
        return ChatResponse(
            reply=llm_reply,
            user=user_data,
            conversation_id=conversation_id,
            stage=req.stage,
            timestamp=datetime.now().isoformat()
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Chat endpoint error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Chat processing failed: {str(e)}"
        )

@app.get("/conversations/{user_id}/{conversation_id}", response_model=ConversationHistoryResponse, tags=["Conversations"])
async def get_conversation(user_id: str, conversation_id: str):
    """
    Retrieve conversation history
    
    Parameters:
    - user_id: UUID of the user
    - conversation_id: ID of the conversation
    """
    
    logger.info(f"📖 Fetching conversation | User: {user_id} | Conversation: {conversation_id}")
    
    try:
        # Verify user
        await verify_user(user_id)
        
        # Fetch conversation
        resp = supabase.table("chat_history").select("*").eq("user_id", user_id).eq("conversation_id", conversation_id).order("created_at", desc=False).execute()
        
        messages = []
        stage = None
        
        if hasattr(resp, "data"):
            data = resp.data
        else:
            data = resp.get("data", []) if isinstance(resp, dict) else []
        
        for msg in data:
            messages.append(ConversationMessage(
                role=msg.get("role", ""),
                content=msg.get("content", ""),
                timestamp=msg.get("created_at", "")
            ))
            if not stage and msg.get("stage"):
                stage = msg["stage"]
        
        logger.info(f"✅ Retrieved {len(messages)} messages from conversation")
        
        return ConversationHistoryResponse(
            user_id=user_id,
            conversation_id=conversation_id,
            messages=messages,
            stage=stage or "general",
            created_at=datetime.now().isoformat()
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error retrieving conversation: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve conversation: {str(e)}"
        )

@app.get("/conversations/{user_id}", tags=["Conversations"])
async def list_user_conversations(user_id: str):
    """
    List all conversations for a user
    
    Parameters:
    - user_id: UUID of the user
    """
    
    logger.info(f"📚 Listing conversations for user: {user_id}")
    
    try:
        # Verify user
        await verify_user(user_id)
        
        # Fetch unique conversations
        resp = supabase.table("chat_history").select("conversation_id, stage, created_at").eq("user_id", user_id).order("created_at", desc=True).execute()
        
        if hasattr(resp, "data"):
            data = resp.data
        else:
            data = resp.get("data", []) if isinstance(resp, dict) else []
        
        # Deduplicate conversations
        conversations = {}
        for msg in data:
            conv_id = msg.get("conversation_id")
            if conv_id and conv_id not in conversations:
                conversations[conv_id] = {
                    "conversation_id": conv_id,
                    "stage": msg.get("stage", "general"),
                    "created_at": msg.get("created_at")
                }
        
        logger.info(f"✅ Found {len(conversations)} conversations")
        
        return {
            "user_id": user_id,
            "conversation_count": len(conversations),
            "conversations": list(conversations.values())
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error listing conversations: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list conversations: {str(e)}"
        )

# Helper function to save conversation
async def save_conversation_message(user_id: str, conversation_id: str, role: str, content: str, stage: str):
    """Save message to conversation history in database"""
    try:
        supabase.table("chat_history").insert({
            "user_id": user_id,
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "stage": stage,
            "created_at": datetime.now().isoformat()
        }).execute()
        logger.debug(f"Message saved | Role: {role} | Length: {len(content)}")
    except Exception as e:
        logger.error(f"Error saving conversation message: {e}")
        raise

# Error handler for rate limit
@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request, exc):
    """Handle rate limit exceeded"""
    logger.warning(f"Rate limit exceeded for {request.client.host}")
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests. Please wait before sending more messages."
    )

if __name__ == '__main__':
    import uvicorn
    logger.info(f"🚀 Starting server at {HOST}:{PORT}")
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info"
    )
