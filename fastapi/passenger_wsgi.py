"""
Phusion Passenger entry-point for GoDaddy / cPanel shared hosting.

Why this file exists:
    Phusion Passenger (the application server cPanel uses for Python apps)
    speaks WSGI only. FastAPI is ASGI (it's built on Starlette). Without a
    bridge Passenger refuses to start the app and serves a generic 500
    "Web application could not be started" page that has no CORS headers —
    which surfaces in the browser as a misleading CORS error.

What it does:
    1. Import the FastAPI `app` from main.py
    2. Wrap it with a2wsgi.ASGIMiddleware so Passenger sees a WSGI callable
    3. Expose that wrapped callable as `application` (Passenger's expected name)

Setup in cPanel:
    Setup Python App → "Application startup file" = passenger_wsgi.py
                    → "Application Entry point"  = application

This file is harmless on non-cPanel hosts — they call `uvicorn main:app`
directly and never load it.
"""

import os
import sys

# Make sure the app directory is on sys.path so `import main` works no
# matter where Passenger CWDs us at boot.
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

from a2wsgi import ASGIMiddleware
from main import app as _asgi_app

# Passenger looks for a module-level callable named `application` by
# convention. The Setup Python App UI uses this name in its "Entry point"
# field unless you override it.
application = ASGIMiddleware(_asgi_app)
