# CheriPic AI Backend Setup Guide

Complete setup and deployment instructions for the CheriAI FastAPI backend.

## Overview

The CheriAI backend is a FastAPI server that integrates with:
- **OpenAI GPT-4o-mini** for intelligent conversation
- **Supabase PostgreSQL** for conversation history and user data
- **Frontend React App** (port 5176) for user interaction

## Prerequisites

- Python 3.9+
- pip (Python package manager)
- Active Supabase project with credentials
- OpenAI API key
- Node.js (for running frontend)

## Installation

### 1. Set Up Python Virtual Environment

```bash
# Navigate to the FastAPI directory
cd ai_integration/fastapi

# Create virtual environment
python3 -m venv venv

# Activate virtual environment
# On macOS/Linux:
source venv/bin/activate

# On Windows:
# venv\Scripts\activate
```

### 2. Install Dependencies

```bash
# Install all required packages
pip install -r requirements.txt

# Verify installation
pip list | grep -E "fastapi|slowapi|supabase|openai"
```

### 3. Configure Environment Variables

```bash
# Edit the .env file with your credentials
nano .env  # or use your preferred editor

# Required variables to fill:
# - SUPABASE_URL: Your Supabase project URL
# - SUPABASE_KEY: Your Supabase service role key (for backend) or anon key
# - OPENAI_API_KEY: Your OpenAI API key
# - FRONTEND_URLS: Your frontend development/production URLs
```

**Example .env configuration:**
```env
SUPABASE_URL=https://abcdef123456.supabase.co
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
OPENAI_API_KEY=sk-proj-abc123def456...
OPENAI_MODEL=gpt-4o-mini
HOST=0.0.0.0
PORT=8000
FRONTEND_URLS=http://localhost:5176,http://127.0.0.1:5176
LOG_LEVEL=INFO
ENVIRONMENT=development
```

> Phone OTP login is handled by **Supabase Auth** (the frontend calls
> `supabase.auth.signInWithOtp` directly). Twilio credentials live in
> the Supabase Dashboard → Authentication → Providers → Phone, not in
> this `.env` and not in this FastAPI service.

### 4. Set Up Database Table

The backend requires a `chat_history` table in Supabase to store conversations.

**Option A: Via Supabase Docs**
1. Go to Supabase → SQL Editor
2. Create a new query
3. Copy and run the SQL from: `database/CREATE_CHAT_HISTORY_TABLE.sql`

**Option B: Via Shell Script**
```bash
# From project root directory
psql "postgresql://postgres:your-password@db.supabase.co:5432/postgres" \
  -f database/CREATE_CHAT_HISTORY_TABLE.sql
```

**Verify the table was created:**
```sql
SELECT * FROM chat_history LIMIT 1;
```

## Running the Backend

### Development Mode (With Auto-Reload)

```bash
# From ai_integration/fastapi directory
python main_enhanced.py

# Or with uvicorn directly
uvicorn main_enhanced:app --host 0.0.0.0 --port 8000 --reload
```

### Production Mode (Gunicorn + Uvicorn)

```bash
# Install gunicorn (if not already installed)
pip install gunicorn

# Run with gunicorn
gunicorn main_enhanced:app --workers 4 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

### Docker Deployment

```bash
# Build Docker image
docker build -t cheripic-ai-backend .

# Run container
docker run -p 8000:8000 --env-file .env cheripic-ai-backend
```

## Running Frontend + Backend Together

### Terminal 1: Start Backend

```bash
cd ai_integration/fastapi
source venv/bin/activate  # or venv\Scripts\activate on Windows
python main_enhanced.py

# Expected output:
# ✅ User verified: Priya
# 🤖 Calling LLM...
# 💾 Conversation saved | ID: conv_...
```

### Terminal 2: Start Frontend

```bash
# From project root
npm install  # If dependencies haven't been installed
npm run dev

# Should show:
# ➜  Local:   http://localhost:5176/
```

### Access the Application

1. Frontend: `http://localhost:5176`
2. Backend API Docs: `http://localhost:8000/docs` (Swagger UI)
3. Backend Alternative Docs: `http://localhost:8000/redoc` (ReDoc)

## API Endpoints

### Health Check

```bash
GET http://localhost:8000/health

Response:
{
  "status": "ok",
  "service": "CheriPic AI Backend",
  "timestamp": "2024-01-15T10:30:00Z"
}
```

### Send Message (Main Endpoint)

```bash
POST http://localhost:8000/chat

Request Body:
{
  "user_id": "c8458310-8079-476f-bbdf-7f22fc70da74",
  "message": "What should I look for in a partner?",
  "stage": "general",
  "context": {
    "current_match": {
      "nick_name": "John",
      "compatibility_score": 85
    }
  }
}

Response:
{
  "reply": "That's a great question! When looking for a partner...",
  "user": {
    "id": "c8458310-8079-476f-bbdf-7f22fc70da74",
    "nick_name": "Priya",
    "gender": "Female",
    "age": 28
  },
  "conversation_id": "conv_c8458310-8079-476f-bbdf-7f22fc70da74_1705315000",
  "stage": "general",
  "timestamp": "2024-01-15T10:30:00Z"
}
```

### Get Conversation History

```bash
GET http://localhost:8000/conversations/{user_id}/{conversation_id}

Example:
GET http://localhost:8000/conversations/c8458310-8079-476f-bbdf-7f22fc70da74/conv_c8458310-8079-476f-bbdf-7f22fc70da74_1705315000

Response:
{
  "user_id": "c8458310-8079-476f-bbdf-7f22fc70da74",
  "conversation_id": "conv_c8458310-8079-476f-bbdf-7f22fc70da74_1705315000",
  "messages": [
    {
      "role": "user",
      "content": "What should I look for in a partner?",
      "timestamp": "2024-01-15T10:25:00Z"
    },
    {
      "role": "assistant",
      "content": "That's a great question!...",
      "timestamp": "2024-01-15T10:30:00Z"
    }
  ],
  "stage": "general",
  "created_at": "2024-01-15T10:25:00Z"
}
```

### List User Conversations

```bash
GET http://localhost:8000/conversations/{user_id}

Example:
GET http://localhost:8000/conversations/c8458310-8079-476f-bbdf-7f22fc70da74

Response:
{
  "user_id": "c8458310-8079-476f-bbdf-7f22fc70da74",
  "conversation_count": 3,
  "conversations": [
    {
      "conversation_id": "conv_c8458310-8079-476f-bbdf-7f22fc70da74_1705315000",
      "stage": "general",
      "created_at": "2024-01-15T10:25:00Z"
    },
    {
      "conversation_id": "conv_c8458310-8079-476f-bbdf-7f22fc70da74_1705314000",
      "stage": "onboarding",
      "created_at": "2024-01-15T10:10:00Z"
    }
  ]
}
```

## Interactive API Testing

### Using Swagger UI

1. Start the backend server
2. Open `http://localhost:8000/docs` in your browser
3. Click on any endpoint to expand it
4. Click "Try it out" button
5. Fill in the parameters/body
6. Click "Execute"

### Using cURL

Test the health endpoint:
```bash
curl -X GET http://localhost:8000/health
```

Test the chat endpoint:
```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "c8458310-8079-476f-bbdf-7f22fc70da74",
    "message": "Hello CheriAI!",
    "stage": "general"
  }'
```

### Using Python Requests

```python
import requests

# Chat request
response = requests.post(
    'http://localhost:8000/chat',
    json={
        'user_id': 'c8458310-8079-476f-bbdf-7f22fc70da74',
        'message': 'What are dealbreakers?',
        'stage': 'golden_questions'
    }
)

print(response.json())
```

## Troubleshooting

### Issue: "ModuleNotFoundError: No module named 'fastapi'"

**Solution:** Make sure virtual environment is activated
```bash
source venv/bin/activate
pip install -r requirements.txt
```

### Issue: "Connection refused" to Supabase

**Solution:** Check your SUPABASE_URL and SUPABASE_KEY in .env
```bash
# Verify Supabase is accessible
curl https://your-project.supabase.co/rest/v1/
```

### Issue: "401 Unauthorized" from OpenAI

**Solution:** Verify your OPENAI_API_KEY is correct in .env
```bash
# Test OpenAI connection
python -c "import openai; print('OK')"
```

### Issue: "CORS error" from frontend

**Solution:** Check FRONTEND_URLS in .env matches your frontend URL
```bash
# Your frontend should be running at one of these:
# - http://localhost:5176
# - http://localhost:5173
# - http://127.0.0.1:5176
```

### Issue: "Rate limit exceeded" (429 error)

**Solution:** The backend is limited to 30 requests/minute by default. Wait a minute before sending more messages, or adjust RATE_LIMIT in .env

### Issue: "User not found in database"

**Solution:** Make sure the user_id exists in your Supabase user_profiles table
```bash
# Check in Supabase SQL Editor
SELECT id, nick_name FROM user_profiles LIMIT 10;
```

## Conversation Stages

The backend supports four conversation stages, each with different prompt context:

**1. Onboarding** (`stage: "onboarding"`)
- New user welcome experience
- Explaining CheriPic philosophy
- Building confidence in the platform
- *Example prompt:* "I'm new to dating apps. How does CheriPic help?"

**2. Golden Questions** (`stage: "golden_questions"`)
- Helping users answer compatibility questions
- Exploring values and life goals
- Encouraging authentic self-reflection
- *Example prompt:* "What does family mean to you?"

**3. Matching** (`stage: "matching"`)
- Understanding compatibility with matches
- Suggesting conversation starters
- Building confidence in reaching out
- *Example prompt:* "Is this person a good match for me?"

**4. General** (`stage: "general"`)
- Free-form relationship coaching
- Dating advice and support
- Emotional support during dating journey
- *Example prompt:* "I'm nervous about my first message"

## Frontend Integration

The frontend uses `src/services/cheriAIService.ts` to communicate with the backend.

**Basic Usage:**

```typescript
import { cheriAIService } from "@/services/cheriAIService";

// Send a message
const response = await cheriAIService.sendMessage(
  userId,
  "What should I include in my profile?",
  "onboarding"
);

console.log(response.reply); // CheriAI's response

// Get conversation history
const history = await cheriAIService.getConversationHistory(
  userId,
  conversationId
);

// List all conversations
const conversations = await cheriAIService.listUserConversations(userId);
```

## Monitoring & Logging

### Log Levels

Set `LOG_LEVEL` in .env to control verbosity:
- `DEBUG`: Detailed debugging information
- `INFO`: General information messages
- `WARNING`: Warning messages
- `ERROR`: Error messages only

### Viewing Logs

```bash
# Start server with verbose logging
LOG_LEVEL=DEBUG python main_enhanced.py

# Or view logs from running server:
# Check terminal where server is running
```

### Database Queries

View conversation patterns:
```sql
-- Most recent conversations
SELECT * FROM conversation_summaries ORDER BY last_message_at DESC LIMIT 10;

-- Conversations by stage
SELECT stage, COUNT(*) as conversation_count 
FROM conversation_summaries 
GROUP BY stage;

-- User activity
SELECT user_id, COUNT(DISTINCT conversation_id) as total_conversations
FROM chat_history
GROUP BY user_id
ORDER BY total_conversations DESC;
```

## Performance Optimization

### Database Indexes

Indexes are automatically created by the migration script for:
- `user_id` (fast user lookups)
- `conversation_id` (fast conversation retrieval)
- `created_at` (sorting by time)
- `stage` (filtering by conversation type)

### Rate Limiting

Current configuration: **30 requests per minute**

To adjust:
1. Edit `.env`: `RATE_LIMIT=50/minute`
2. Or modify in `main_enhanced.py`: `@limiter.limit("50/minute")`

### Connection Pooling

Supabase connections are pooled by the `supabase-py` client. No additional configuration needed.

## Deployment Checklist

- [ ] Python 3.9+ installed
- [ ] Dependencies installed (`pip install -r requirements.txt`)
- [ ] .env file configured with all credentials
- [ ] Supabase `chat_history` table created
- [ ] Frontend URLS added to FRONTEND_URLS in .env
- [ ] Backend running without errors
- [ ] Health endpoint responding (`/health`)
- [ ] Chat endpoint tested with sample user
- [ ] Conversation history retrieval working

## Support & Debugging

For detailed issues:

1. **Check logs**: Look at terminal output where backend is running
2. **Test health**: `curl http://localhost:8000/health`
3. **Verify env**: Print env vars (without secrets): `env | grep -v KEY`
4. **Check Supabase**: Ensure tables exist and RLS policies are correct
5. **Monitor API usage**: Check OpenAI API dashboard for quota issues
6. **Review database**: Query `chat_history` table to verify data is saving

## Related Files

- **API Logic**: [ai_integration/fastapi/main_enhanced.py](../fastapi/main_enhanced.py)
- **Prompt Templates**: [ai_integration/fastapi/cheriai_prompts.py](../fastapi/cheriai_prompts.py)
- **Frontend Service**: [src/services/cheriAIService.ts](../../src/services/cheriAIService.ts)
- **Database Schema**: [database/CREATE_CHAT_HISTORY_TABLE.sql](../../database/CREATE_CHAT_HISTORY_TABLE.sql)
- **Dependencies**: [ai_integration/fastapi/requirements.txt](../fastapi/requirements.txt)

## Next Steps

1. ✅ Start the backend server
2. ✅ Start the frontend development server
3. ✅ Go to `http://localhost:5176`
4. ✅ Login with Priya's account
5. ✅ Click the CheriAI icon to start chatting
6. ✅ Monitor logs and test different conversation stages
7. 🚀 Deploy to production when ready

---

**Last Updated:** 2024-01-15
**Version:** 1.0.0
**Status:** Production Ready
