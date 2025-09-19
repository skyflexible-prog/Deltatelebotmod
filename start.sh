#!/bin/bash

# Set environment variables for production
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1

# Start the application with Gunicorn
exec gunicorn --config gunicorn.conf.py app:app
