import os
import asyncio
import logging
import hashlib
import hmac
import time
import json
import requests
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from flask import Flask, request, jsonify
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import telegram
from telegram.ext import Application, CommandHandler, ContextTypes
import pytz
import signal
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

@dataclass
class OptionContract:
    symbol: str
    product_id: int
    strike_price: float
    option_type: str  # 'call' or 'put'
    expiry_date: str
    mark_price: float
    bid_price: float
    ask_price: float

@dataclass
class TradeResult:
    success: bool
    order_id: Optional[int] = None
    error_message: Optional[str] = None
    premium_collected: Optional[float] = None

class DeltaExchangeAPI:
    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://api.india.delta.exchange"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.session = requests.Session()
        
    def generate_signature(self, message: str) -> str:
        """Generate HMAC signature for API authentication"""
        message_bytes = bytes(message, 'utf-8')
        secret_bytes = bytes(self.api_secret, 'utf-8')
        hash_obj = hmac.new(secret_bytes, message_bytes, hashlib.sha256)
        return hash_obj.hexdigest()
    
    def get_headers(self, method: str, path: str, query_string: str = "", payload: str = "") -> Dict[str, str]:
        """Generate authentication headers"""
        timestamp = str(int(time.time()))
        signature_data = method + timestamp + path + query_string + payload
        signature = self.generate_signature(signature_data)
        
        return {
            'api-key': self.api_key,
            'timestamp': timestamp,
            'signature': signature,
            'User-Agent': 'python-trading-bot',
            'Content-Type': 'application/json'
        }
    
    def make_request(self, method: str, endpoint: str, params: Dict = None, data: Dict = None) -> Dict:
        """Make authenticated API request"""
        url = f"{self.base_url}{endpoint}"
        query_string = ""
        payload = ""
        
        if params:
            query_string = "?" + "&".join([f"{k}={v}" for k, v in params.items()])
        
        if data:
            payload = json.dumps(data)
        
        headers = self.get_headers(method, endpoint, query_string, payload)
        
        try:
            if method == "GET":
                response = self.session.get(url, params=params, headers=headers, timeout=30)
            elif method == "POST":
                response = self.session.post(url, data=payload, params=params, headers=headers, timeout=30)
            elif method == "PUT":
                response = self.session.put(url, data=payload, params=params, headers=headers, timeout=30)
            elif method == "DELETE":
                response = self.session.delete(url, params=params, headers=headers, timeout=30)
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            return {"success": False, "error": str(e)}
    
    def get_btc_spot_price(self) -> Optional[float]:
        """Get current BTC spot price from index"""
        try:
            # Get BTC perpetual ticker which contains spot price
            response = self.make_request("GET", "/v2/tickers/BTCUSD")
            if response.get("success") and "result" in response:
                spot_price = float(response["result"]["spot_price"])
                logger.info(f"Current BTC spot price: ${spot_price}")
                return spot_price
            else:
                logger.error(f"Failed to get BTC spot price: {response}")
                return None
        except Exception as e:
            logger.error(f"Error getting BTC spot price: {e}")
            return None
    
    def get_option_chain(self, expiry_date: str) -> List[OptionContract]:
        """Get BTC option chain for specific expiry date"""
        try:
            params = {
                "contract_types": "call_options,put_options",
                "underlying_asset_symbols": "BTC",
                "expiry_date": expiry_date
            }
            
            response = self.make_request("GET", "/v2/tickers", params=params)
            
            if not response.get("success"):
                logger.error(f"Failed to get option chain: {response}")
                return []
            
            options = []
            for ticker in response.get("result", []):
                try:
                    option_type = "call" if ticker["symbol"].startswith("C-") else "put"
                    strike_price = float(ticker["strike_price"])
                    
                    # Get bid/ask prices
                    quotes = ticker.get("quotes", {})
                    bid_price = float(quotes.get("best_bid", 0))
                    ask_price = float(quotes.get("best_ask", 0))
                    mark_price = float(ticker.get("mark_price", 0))
                    
                    option = OptionContract(
                        symbol=ticker["symbol"],
                        product_id=ticker["product_id"],
                        strike_price=strike_price,
                        option_type=option_type,
                        expiry_date=expiry_date,
                        mark_price=mark_price,
                        bid_price=bid_price,
                        ask_price=ask_price
                    )
                    options.append(option)
                    
                except (KeyError, ValueError) as e:
                    logger.warning(f"Error parsing option data: {e}")
                    continue
            
            logger.info(f"Retrieved {len(options)} options for {expiry_date}")
            return options
            
        except Exception as e:
            logger.error(f"Error getting option chain: {e}")
            return []
    
    def find_strangle_strikes(self, spot_price: float, options: List[OptionContract]) -> Tuple[Optional[OptionContract], Optional[OptionContract]]:
        """Find call and put options 1% away from spot price"""
        target_call_strike = spot_price * 1.01  # 1% above spot
        target_put_strike = spot_price * 0.99   # 1% below spot
        
        # Find closest strikes
        call_option = None
        put_option = None
        
        call_options = [opt for opt in options if opt.option_type == "call"]
        put_options = [opt for opt in options if opt.option_type == "put"]
        
        if call_options:
            call_option = min(call_options, key=lambda x: abs(x.strike_price - target_call_strike))
        
        if put_options:
            put_option = min(put_options, key=lambda x: abs(x.strike_price - target_put_strike))
        
        if call_option and put_option:
            logger.info(f"Selected strangle: Call {call_option.strike_price} @ ${call_option.mark_price}, Put {put_option.strike_price} @ ${put_option.mark_price}")
        
        return call_option, put_option
    
    def place_order(self, product_id: int, side: str, size: int = 1, order_type: str = "market_order") -> TradeResult:
        """Place an order on Delta Exchange"""
        try:
            order_data = {
                "product_id": product_id,
                "size": size,
                "side": side,
                "order_type": order_type
            }
            
            response = self.make_request("POST", "/v2/orders", data=order_data)
            
            if response.get("success"):
                order_id = response["result"]["id"]
                logger.info(f"Order placed successfully: ID {order_id}")
                return TradeResult(success=True, order_id=order_id)
            else:
                error_msg = response.get("error", {}).get("message", "Unknown error")
                logger.error(f"Order placement failed: {error_msg}")
                return TradeResult(success=False, error_message=error_msg)
                
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return TradeResult(success=False, error_message=str(e))
    
    def get_positions(self) -> List[Dict]:
        """Get current positions"""
        try:
            response = self.make_request("GET", "/v2/positions")
            if response.get("success"):
                return response.get("result", [])
            return []
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []

class ShortStrangleBot:
    def __init__(self, api_key: str, api_secret: str, telegram_token: str, chat_id: str):
        self.api = DeltaExchangeAPI(api_key, api_secret)
        self.telegram_token = telegram_token
        self.chat_id = chat_id
        self.active_positions = {}
        self.stop_loss_orders = {}
        self.scheduler = None
        self.telegram_app = None
        self.bot_thread = None
        self.telegram_enabled = False
        
        # Initialize Telegram bot
        self.setup_telegram_bot()
        
    def setup_telegram_bot(self):
        """Setup Telegram bot with proper error handling"""
        try:
            if not self.telegram_token or not self.chat_id:
                logger.warning("Telegram token or chat ID not provided, Telegram features disabled")
                return
            
            # Test the token first
            test_url = f"https://api.telegram.org/bot{self.telegram_token}/getMe"
            test_response = requests.get(test_url, timeout=10)
            
            if test_response.status_code != 200:
                logger.error(f"Invalid Telegram token: {test_response.text}")
                return
            
            # Create the application
            self.telegram_app = Application.builder().token(self.telegram_token).build()
            
            # Add command handlers
            self.telegram_app.add_handler(CommandHandler("start", self.telegram_start))
            self.telegram_app.add_handler(CommandHandler("status", self.telegram_status))
            self.telegram_app.add_handler(CommandHandler("execute", self.telegram_execute_trade))
            self.telegram_app.add_handler(CommandHandler("positions", self.telegram_positions))
            self.telegram_app.add_handler(CommandHandler("help", self.telegram_help))
            
            self.telegram_enabled = True
            logger.info("Telegram bot setup completed successfully")
            
        except Exception as e:
            logger.error(f"Failed to setup Telegram bot: {e}")
            self.telegram_app = None
            self.telegram_enabled = False
    
    def send_telegram_message_sync(self, message: str):
        """Send message via Telegram synchronously"""
        if not self.telegram_enabled:
            logger.info(f"Telegram disabled, would send: {message}")
            return
            
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            response = requests.post(url, data=data, timeout=10)
            if response.status_code == 200:
                logger.info("Telegram message sent successfully")
            else:
                logger.error(f"Failed to send Telegram message: {response.text}")
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
    
    async def telegram_start(self, update, context: ContextTypes.DEFAULT_TYPE):
        """Telegram /start command"""
        message = "🤖 <b>Delta Exchange Short Strangle Bot</b>\n\n"
        message += "Available commands:\n"
        message += "/status - Check bot status\n"
        message += "/execute - Execute manual trade\n"
        message += "/positions - View current positions\n"
        message += "/help - Show this help message\n\n"
        message += "⚠️ <b>Risk Warning:</b> Short strangle strategies involve unlimited risk potential."
        
        await update.message.reply_text(message, parse_mode='HTML')
    
    async def telegram_status(self, update, context: ContextTypes.DEFAULT_TYPE):
        """Telegram /status command"""
        spot_price = self.api.get_btc_spot_price()
        positions = self.api.get_positions()
        
        message = f"📊 <b>Bot Status</b>\n\n"
        message += f"BTC Spot Price: ${spot_price:.2f}\n" if spot_price else "BTC Spot Price: Unable to fetch\n"
        message += f"Active Positions: {len([p for p in positions if float(p.get('size', 0)) != 0])}\n"
        message += f"Next Auto Trade: Daily at 7:00 AM UTC\n"
        
        await update.message.reply_text(message, parse_mode='HTML')
    
    async def telegram_execute_trade(self, update, context: ContextTypes.DEFAULT_TYPE):
        """Telegram /execute command"""
        await update.message.reply_text("🔄 Executing manual short strangle trade...")
        
        # Execute trade in sync mode since we're calling from async context
        result = self.execute_short_strangle_sync()
        
        if result["success"]:
            message = f"✅ <b>Trade Executed Successfully</b>\n\n"
            message += f"Spot Price: ${result['spot_price']:.2f}\n"
            message += f"Call Strike: ${result['call_strike']:.0f}\n"
            message += f"Put Strike: ${result['put_strike']:.0f}\n"
            message += f"Total Premium: ${result['total_premium']:.2f}\n"
            message += f"Stop Loss: ${result['stop_loss']:.2f}"
        else:
            message = f"❌ <b>Trade Failed</b>\n\n"
            message += f"Error: {result['error']}"
        
        await update.message.reply_text(message, parse_mode='HTML')
    
    async def telegram_positions(self, update, context: ContextTypes.DEFAULT_TYPE):
        """Telegram /positions command"""
        positions = self.api.get_positions()
        
        if not positions:
            await update.message.reply_text("📈 No active positions")
            return
        
        message = "📈 <b>Current Positions</b>\n\n"
        for pos in positions:
            if float(pos.get('size', 0)) != 0:
                message += f"Symbol: {pos.get('product_symbol', 'N/A')}\n"
                message += f"Size: {pos.get('size', 'N/A')}\n"
                message += f"Entry Price: ${float(pos.get('entry_price', 0)):.2f}\n"
                message += f"Mark Price: ${float(pos.get('mark_price', 0)):.2f}\n"
                message += f"PnL: ${float(pos.get('unrealized_pnl', 0)):.2f}\n\n"
        
        await update.message.reply_text(message, parse_mode='HTML')
    
    async def telegram_help(self, update, context: ContextTypes.DEFAULT_TYPE):
        """Telegram /help command"""
        message = "🤖 <b>Short Strangle Bot Help</b>\n\n"
        message += "<b>Strategy:</b>\n"
        message += "• Sells call and put options 1% away from BTC spot price\n"
        message += "• Uses same-day expiry options\n"
        message += "• Stop-loss at 1x premium collected\n\n"
        message += "<b>Schedule:</b>\n"
        message += "• Auto-executes daily at 7:00 AM UTC (12:30 PM IST)\n\n"
        message += "<b>Commands:</b>\n"
        message += "/status - Bot and market status\n"
        message += "/execute - Manual trade execution\n"
        message += "/positions - View current positions\n"
        message += "/help - This help message\n\n"
        message += "⚠️ <b>Warning:</b> This strategy involves unlimited risk!"
        
        await update.message.reply_text(message, parse_mode='HTML')
    
    def get_today_expiry_date(self) -> str:
        """Get today's date in DD-MM-YYYY format for same-day expiry"""
        return datetime.now(timezone.utc).strftime("%d-%m-%Y")
    
    def execute_short_strangle_sync(self) -> Dict:
        """Execute the short strangle strategy synchronously"""
        try:
            logger.info("Starting short strangle execution")
            
            # Get current BTC spot price
            spot_price = self.api.get_btc_spot_price()
            if not spot_price:
                return {"success": False, "error": "Unable to fetch BTC spot price"}
            
            # Get today's expiry options
            expiry_date = self.get_today_expiry_date()
            options = self.api.get_option_chain(expiry_date)
            
            if not options:
                return {"success": False, "error": f"No options found for expiry {expiry_date}"}
            
            # Find appropriate strikes (1% away from spot)
            call_option, put_option = self.api.find_strangle_strikes(spot_price, options)
            
            if not call_option or not put_option:
                return {"success": False, "error": "Unable to find suitable option strikes"}
            
            # Execute trades (sell call and put)
            call_result = self.api.place_order(call_option.product_id, "sell", 1)
            if not call_result.success:
                return {"success": False, "error": f"Call order failed: {call_result.error_message}"}
            
            put_result = self.api.place_order(put_option.product_id, "sell", 1)
            if not put_result.success:
                # Try to close the call position if put fails
                self.api.place_order(call_option.product_id, "buy", 1)
                return {"success": False, "error": f"Put order failed: {put_result.error_message}"}
            
            # Calculate total premium collected and stop loss
            total_premium = call_option.mark_price + put_option.mark_price
            stop_loss_level = total_premium  # 1x premium as stop loss
            
            # Store position info for monitoring
            position_key = f"{expiry_date}_{int(spot_price)}"
            self.active_positions[position_key] = {
                "call_option": call_option,
                "put_option": put_option,
                "call_order_id": call_result.order_id,
                "put_order_id": put_result.order_id,
                "entry_time": datetime.now(timezone.utc),
                "spot_price": spot_price,
                "total_premium": total_premium,
                "stop_loss": stop_loss_level
            }
            
            result = {
                "success": True,
                "spot_price": spot_price,
                "call_strike": call_option.strike_price,
                "put_strike": put_option.strike_price,
                "total_premium": total_premium,
                "stop_loss": stop_loss_level,
                "expiry_date": expiry_date
            }
            
            logger.info(f"Short strangle executed successfully: {result}")
            
            # Send Telegram notification
            message = f"✅ <b>Short Strangle Executed</b>\n\n"
            message += f"BTC Spot: ${spot_price:.2f}\n"
            message += f"Call Strike: ${call_option.strike_price:.0f}\n"
            message += f"Put Strike: ${put_option.strike_price:.0f}\n"
            message += f"Premium Collected: ${total_premium:.2f}\n"
            message += f"Stop Loss: ${stop_loss_level:.2f}\n"
            message += f"Expiry: {expiry_date}"
            
            self.send_telegram_message_sync(message)
            
            return result
            
        except Exception as e:
            logger.error(f"Error executing short strangle: {e}")
            self.send_telegram_message_sync(f"❌ <b>Trade Execution Failed</b>\n\nError: {str(e)}")
            return {"success": False, "error": str(e)}
    
    def monitor_positions_sync(self):
        """Monitor positions for stop loss synchronously"""
        try:
            positions = self.api.get_positions()
            
            for position_key, position_info in list(self.active_positions.items()):
                # Check if position still exists and monitor P&L
                call_option = position_info["call_option"]
                put_option = position_info["put_option"]
                stop_loss = position_info["stop_loss"]
                
                # Find current positions
                call_pos = next((p for p in positions if p.get("product_id") == call_option.product_id), None)
                put_pos = next((p for p in positions if p.get("product_id") == put_option.product_id), None)
                
                if call_pos and put_pos:
                    total_pnl = float(call_pos.get("unrealized_pnl", 0)) + float(put_pos.get("unrealized_pnl", 0))
                    
                    # Check stop loss (negative PnL exceeds stop loss level)
                    if total_pnl <= -stop_loss:
                        logger.warning(f"Stop loss triggered for {position_key}: PnL ${total_pnl:.2f}")
                        
                        # Close positions
                        self.api.place_order(call_option.product_id, "buy", 1)
                        self.api.place_order(put_option.product_id, "buy", 1)
                        
                        # Send notification
                        message = f"🛑 <b>Stop Loss Triggered</b>\n\n"
                        message += f"Position: {position_key}\n"
                        message += f"Total P&L: ${total_pnl:.2f}\n"
                        message += f"Stop Loss Level: ${stop_loss:.2f}"
                        
                        self.send_telegram_message_sync(message)
                        
                        # Remove from active positions
                        del self.active_positions[position_key]
                
        except Exception as e:
            logger.error(f"Error monitoring positions: {e}")
    
    def start_scheduler(self):
        """Start the scheduled trading"""
        self.scheduler = BackgroundScheduler()
        
        # Schedule daily execution at 7:00 AM UTC
        self.scheduler.add_job(
            func=self.execute_short_strangle_sync,
            trigger=CronTrigger(hour=7, minute=0, timezone=pytz.UTC),
            id='daily_strangle',
            name='Daily Short Strangle Execution'
        )
        
        # Schedule position monitoring every 5 minutes
        self.scheduler.add_job(
            func=self.monitor_positions_sync,
            trigger='interval',
            minutes=5,
            id='position_monitor',
            name='Position Monitoring'
        )
        
        self.scheduler.start()
        logger.info("Scheduler started - Daily execution at 7:00 AM UTC")
        
        return self.scheduler
    
    def start_telegram_bot(self):
        """Start Telegram bot in a separate thread"""
        if not self.telegram_enabled or not self.telegram_app:
            logger.warning("Telegram bot not enabled or not properly initialized")
            return
            
        def run_bot():
            try:
                # Create new event loop for this thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                # Run the bot
                logger.info("Starting Telegram bot polling...")
                self.telegram_app.run_polling(drop_pending_updates=True)
                
            except Exception as e:
                logger.error(f"Telegram bot error: {e}")
            finally:
                try:
                    loop.close()
                except:
                    pass
        
        self.bot_thread = threading.Thread(target=run_bot, daemon=True)
        self.bot_thread.start()
        logger.info("Telegram bot thread started")
    
    def shutdown(self):
        """Graceful shutdown"""
        logger.info("Starting bot shutdown...")
        
        if self.scheduler:
            try:
                self.scheduler.shutdown(wait=False)
                logger.info("Scheduler shutdown completed")
            except Exception as e:
                logger.error(f"Error shutting down scheduler: {e}")
        
        if self.telegram_app and self.telegram_enabled:
            try:
                # Stop the telegram app
                if hasattr(self.telegram_app, 'stop'):
                    self.telegram_app.stop()
                logger.info("Telegram bot shutdown completed")
            except Exception as e:
                logger.error(f"Error shutting down telegram bot: {e}")
        
        logger.info("Bot shutdown completed")

# Flask app for render.com deployment
app = Flask(__name__)

# Global bot instance
bot_instance = None

@app.route('/', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "service": "Delta Exchange Short Strangle Bot",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "telegram_enabled": bot_instance.telegram_enabled if bot_instance else False
    })

@app.route('/execute', methods=['POST'])
def manual_execute():
    """Manual execution endpoint"""
    try:
        if bot_instance:
            result = bot_instance.execute_short_strangle_sync()
            return jsonify(result)
        return jsonify({"error": "Bot not initialized"}), 500
    except Exception as e:
        logger.error(f"Manual execution error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/status', methods=['GET'])
def bot_status():
    """Bot status endpoint"""
    try:
        if bot_instance:
            spot_price = bot_instance.api.get_btc_spot_price()
            positions = bot_instance.api.get_positions()
            active_positions = len([p for p in positions if float(p.get('size', 0)) != 0])
            
            return jsonify({
                "status": "running",
                "btc_spot_price": spot_price,
                "active_positions": active_positions,
                "telegram_enabled": bot_instance.telegram_enabled,
                "scheduler_running": bot_instance.scheduler.running if bot_instance.scheduler else False
            })
        return jsonify({"error": "Bot not initialized"}), 500
    except Exception as e:
        logger.error(f"Status check error: {e}")
        return jsonify({"error": str(e)}), 500

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, shutting down...")
    if bot_instance:
        bot_instance.shutdown()
    sys.exit(0)

def initialize_bot():
    """Initialize and start the bot"""
    global bot_instance
    
    # Get environment variables
    API_KEY = os.getenv('DELTA_API_KEY')
    API_SECRET = os.getenv('DELTA_API_SECRET')
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    
    if not all([API_KEY, API_SECRET]):
        logger.error("Missing required Delta Exchange API credentials")
        return False
    
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram credentials not provided, Telegram features will be disabled")
    
    try:
        # Initialize bot
        bot_instance = ShortStrangleBot(API_KEY, API_SECRET, TELEGRAM_TOKEN or "", CHAT_ID or "")
        
        # Start scheduler
        bot_instance.start_scheduler()
        
        # Start Telegram bot if enabled
        if bot_instance.telegram_enabled:
            bot_instance.start_telegram_bot()
        
        logger.info("Short Strangle Bot started successfully")
        
        # Send startup notification if Telegram is enabled
        if bot_instance.telegram_enabled:
            bot_instance.send_telegram_message_sync(
                "🚀 <b>Short Strangle Bot Started</b>\n\n"
                "Bot is now running and will execute trades daily at 7:00 AM UTC.\n"
                "Use /help for available commands."
            )
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to initialize bot: {e}")
        return False

# Register signal handlers for graceful shutdown
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

if __name__ == '__main__':
    # For local testing
    initialize_bot()
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
