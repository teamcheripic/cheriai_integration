==============================================================================
CheriPic AI Backend — How to run
==============================================================================
The active service lives in ./fastapi/ (FastAPI + OpenAI + Supabase).
The top-level main.py / app.py are the older Flask prototype — leave them be.

------------------------------------------------------------------------------
Prerequisites
------------------------------------------------------------------------------
• Python 3.10 or newer       — check with: python3 --version
• Working Supabase project   — URL + secret key
• OpenAI API key             — sk-...  (mock LLM falls back if missing)

------------------------------------------------------------------------------
1. First-time setup
------------------------------------------------------------------------------
# From the repo root:
cd ai_integration/fastapi

# Create a virtual environment (only the first time)
python3 -m venv venv

# Activate it (do this every shell session)
#   macOS / Linux:
source venv/bin/activate
#   Windows (PowerShell):
#   venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

------------------------------------------------------------------------------
2. Configure .env  (already exists at ai_integration/fastapi/.env)
------------------------------------------------------------------------------
Required keys (current values are pre-filled — update if rotated):

  SUPABASE_URL        = https://<your-project-ref>.supabase.co
  SUPABASE_KEY        = sb_secret_...        # server-only secret key
  OPENAI_API_KEY      = sk-proj-...          # leave a placeholder to use mock LLM
  OPENAI_MODEL        = gpt-4o-mini
  HOST                = 0.0.0.0
  PORT                = 8000
  FRONTEND_URLS       = http://localhost:5173,http://localhost:5174,...
  RATE_LIMIT          = 30/minute
  LOG_LEVEL            = INFO
  ENVIRONMENT          = development

Never commit .env. Never put SUPABASE_KEY in any frontend code.

------------------------------------------------------------------------------
3. Run the server
------------------------------------------------------------------------------
# Option A — dev mode with auto-reload (recommended while coding)
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Option B — plain run (uses HOST/PORT from .env)
python main.py

You should see:
  INFO:     Uvicorn running on http://0.0.0.0:8000
  INFO:     Application startup complete.

------------------------------------------------------------------------------
4. Verify it's up
------------------------------------------------------------------------------
# Health check
curl http://localhost:8000/health

# Interactive API docs (open in browser)
open http://localhost:8000/docs

Endpoints available:
  GET  /                                              # info
  GET  /health                                        # liveness
  POST /chat                                          # CheriAI conversation
  GET  /conversations/{user_id}                       # list a user's threads
  GET  /conversations/{user_id}/{conversation_id}     # full thread

------------------------------------------------------------------------------
5. Stopping / next time
------------------------------------------------------------------------------
• Ctrl+C in the terminal stops the server.
• Next time:
    cd ai_integration/fastapi
    source venv/bin/activate
    uvicorn main:app --reload

------------------------------------------------------------------------------
File layout (fastapi/)
------------------------------------------------------------------------------
  main.py              FastAPI app, routes, lifespan, CORS, rate limit
  llm_client.py        OpenAI client (returns "[MOCK LLM] ..." if key missing)
  supabase_client.py   Persists chat_history to Supabase
  cheriai_prompts.py   System prompts and onboarding stages
  requirements.txt     Pinned dependency versions
  .env                 Local secrets (gitignored)
  SETUP_AND_RUN.md     Longer reference doc

------------------------------------------------------------------------------
Common issues
------------------------------------------------------------------------------
• "command not found: uvicorn"          → venv not activated. Run: source venv/bin/activate
• CORS error from the frontend          → add the dev URL to FRONTEND_URLS in .env, restart
• 401 from Supabase on chat write       → SUPABASE_KEY must be the SECRET key, not anon
• "[MOCK LLM] ..." replies              → OPENAI_API_KEY is a placeholder; replace it
• Port already in use                   → change PORT in .env or kill the other process
