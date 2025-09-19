import os
from waitress import serve
from app import app, initialize_bot

if __name__ == '__main__':
    # Initialize the bot
    initialize_bot()
    
    # Get port from environment
    port = int(os.environ.get('PORT', 10000))
    
    # Serve the application
    serve(app, host='0.0.0.0', port=port, threads=4)
  
