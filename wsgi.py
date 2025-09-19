from main import app, initialize_bot

# Initialize the bot when the WSGI application starts
if initialize_bot():
    print("Bot initialized successfully")
else:
    print("Failed to initialize bot")

if __name__ == "__main__":
    app.run()
  
