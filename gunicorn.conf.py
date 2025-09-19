import os

# Server socket
bind = f"0.0.0.0:{os.environ.get('PORT', 10000)}"

# Worker processes - Simple sync workers
workers = 2
worker_class = "sync"
timeout = 120
keepalive = 2

# Restart workers after this many requests
max_requests = 1000
max_requests_jitter = 100

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Process naming
proc_name = "delta-strangle-bot"

# Server mechanics
preload_app = True
daemon = False

# Application
wsgi_module = "app:app"
