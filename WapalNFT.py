import requests
import tweepy
import logging
import os
import time
import queue
import pickle
from datetime import datetime, timezone
from threading import Thread, Lock
from pathlib import Path

# --- CONFIGURATION ---
CONFIG = {
    # Twitter API Keys
    "API_KEY": "YOUR TWITTER API KEY",
    "API_SECRET": "YOUR API SECRET KEY",
    "ACCESS_TOKEN": "YOUR ACCESS KEY",
    "ACCESS_SECRET": "YOUR ACCESS SECRET KEY",

    # NFT Collection
    "COLLECTION_SLUG": "Your Collection slug",

    # System
    "LOG_FILE": "nft_bot.log",
    "TEMP_IMAGE": "temp_nft.jpg",
    "PROCESSED_SALES_FILE": "processed_sales.txt",
    "QUEUE_FILE": "sales_queue.pkl",
    
    # API Endpoints
    "COINGECKO_URL": "https://api.coingecko.com/api/v3/simple/price?ids=aptos&vs_currencies=usd",
    "NFT_API_URL": "WAPAL API: Get it from them",
    
    # Timing
    "CHECK_INTERVAL": 180,  # 3 minutes between checks
    "TWEET_INTERVAL": 300   # 5 minutes between tweets
}

# --- INITIALIZATION ---
if os.name == 'nt':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(CONFIG["LOG_FILE"], encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- STATUS MESSAGES ---
def log_status(message):
    """Special formatted status messages"""
    logger.info(f"🔄 STATUS: {message}")
    print(f"\n[STATUS] {datetime.now().strftime('%H:%M:%S')} - {message}\n")

# Initialize Twitter API clients
try:
    log_status("Initializing Twitter API connections...")
    auth = tweepy.OAuth1UserHandler(
        CONFIG["API_KEY"], CONFIG["API_SECRET"],
        CONFIG["ACCESS_TOKEN"], CONFIG["ACCESS_SECRET"]
    )
    api_v1 = tweepy.API(auth, wait_on_rate_limit=True)
    client_v2 = tweepy.Client(
        consumer_key=CONFIG["API_KEY"],
        consumer_secret=CONFIG["API_SECRET"],
        access_token=CONFIG["ACCESS_TOKEN"],
        access_token_secret=CONFIG["ACCESS_SECRET"]
    )
    log_status("Twitter API initialized successfully")
except Exception as e:
    logger.critical(f"Twitter client initialization failed: {e}")
    exit(1)

# Global variables
sales_queue = queue.Queue()
processed_sales = set()
processing_active = True
bot_start_time = datetime.now(timezone.utc)
queue_lock = Lock()

# --- HELPER FUNCTIONS ---
def load_processed_sales():
    """Load set of already processed sales from file"""
    log_status("Loading previously processed sales...")
    processed = set()
    try:
        if Path(CONFIG["PROCESSED_SALES_FILE"]).exists():
            with open(CONFIG["PROCESSED_SALES_FILE"], 'r') as f:
                processed.update(line.strip() for line in f if line.strip())
            log_status(f"Loaded {len(processed)} previously processed sales")
    except Exception as e:
        logger.error(f"Error loading processed sales: {e}")
    return processed

def save_processed_sale(sale_id):
    """Add a sale ID to the processed set and save to file"""
    try:
        with open(CONFIG["PROCESSED_SALES_FILE"], 'a') as f:
            f.write(f"{sale_id}\n")
    except Exception as e:
        logger.error(f"Error saving processed sale: {e}")

def save_queue():
    """Save current queue to disk"""
    try:
        with queue_lock:
            temp_queue = list(sales_queue.queue)
            with open(CONFIG["QUEUE_FILE"], 'wb') as f:
                pickle.dump(temp_queue, f)
            log_status(f"Queue saved with {len(temp_queue)} sales pending")
    except Exception as e:
        logger.error(f"Failed to save queue: {e}")

def load_queue():
    """Load saved queue from disk and filter already processed sales"""
    log_status("Checking for saved queue...")
    try:
        if Path(CONFIG["QUEUE_FILE"]).exists():
            with open(CONFIG["QUEUE_FILE"], 'rb') as f:
                saved_queue = pickle.load(f)
            
            initial_count = len(saved_queue)
            filtered_count = 0
            
            with queue_lock:
                for item in saved_queue:
                    if item.get('transactionVersion') not in processed_sales:
                        sales_queue.put(item)
                    else:
                        filtered_count += 1
            
            os.remove(CONFIG["QUEUE_FILE"])
            log_status(f"Queue loaded - {initial_count - filtered_count} valid sales, {filtered_count} duplicates filtered")
    except Exception as e:
        logger.error(f"Failed to load queue: {e}")

def fetch_recent_sales():
    """Fetch recent NFT sales from API"""
    log_status("Fetching recent sales from API...")
    params = {
        "page": 1,
        "take": 20,
        "collectionSlug": CONFIG["COLLECTION_SLUG"],
        "type": "sales",
        "marketplace": "wapal"
    }
    try:
        response = requests.get(CONFIG["NFT_API_URL"], params=params, timeout=15)
        response.raise_for_status()
        sales = response.json()
        log_status(f"API returned {len(sales)} sales")
        return sales
    except Exception as e:
        logger.error(f"Failed to fetch sales: {e}")
        return []

def process_image(image_url):
    """Download and process NFT image for Twitter"""
    log_status(f"Processing image: {image_url[:50]}...")
    try:
        response = requests.get(image_url, stream=True, timeout=10)
        response.raise_for_status()
        with open(CONFIG["TEMP_IMAGE"], 'wb') as f:
            for chunk in response.iter_content(1024):
                f.write(chunk)
        media = api_v1.media_upload(CONFIG["TEMP_IMAGE"])
        log_status("Image processed successfully")
        return media.media_id
    except Exception as e:
        logger.warning(f"Image processing failed: {e}")
        return None
    finally:
        if os.path.exists(CONFIG["TEMP_IMAGE"]):
            os.remove(CONFIG["TEMP_IMAGE"])

def get_apt_price():
    """Get current APT price from CoinGecko"""
    log_status("Fetching current APT price...")
    try:
        response = requests.get(CONFIG["COINGECKO_URL"], timeout=10)
        response.raise_for_status()
        price = float(response.json()['aptos']['usd'])
        log_status(f"Current APT price: ${price:.2f}")
        return price
    except Exception as e:
        logger.error(f"Failed to fetch APT price: {e}")
        return None

# --- CORE FUNCTIONS ---
def create_tweet(sale):
    """Create and post tweet about NFT sale"""
    tx_version = sale.get("transactionVersion")
    if not tx_version:
        logger.error("Sale missing transaction version")
        return False
    
    log_status(f"Preparing tweet for sale {tx_version}")
    
    # Mark as processed immediately
    save_processed_sale(tx_version)
    processed_sales.add(tx_version)
    
    explorer_link = f"https://explorer.aptoslabs.com/txn/{tx_version}?network=mainnet"
    
    # Get APT price
    apt_usd_rate = get_apt_price() or 4.50  # Fallback price
    
    # Calculate amounts
    apt_amount = int(sale.get('price', 0)) / 10**8
    usd_amount = apt_amount * apt_usd_rate
    
    # Format tweet
    token_name = sale.get('tokenName', 'Unknown')
    token_number = token_name.split('#')[-1] if '#' in token_name else '?'
    
    tweet_text = (
        f"🐧 Aptos Penguins #{token_number} bought for {apt_amount:.2f} $APT (💵 ${usd_amount:.2f})\n"
        f"by {sale.get('buyer', '')[:6]}...{sale.get('buyer', '')[-4:]} "
        f"from {sale.get('seller', '')[:6]}...{sale.get('seller', '')[-4:]}\n"
        f"{explorer_link}\n"
        f"#Aptos #AptosPenguins #NFT"
    )

    # Process image
    media_id = process_image(sale["tokenImageUri"]) if sale.get("tokenImageUri") else None

    # Post tweet
    try:
        log_status("Posting tweet...")
        client_v2.create_tweet(text=tweet_text, media_ids=[media_id] if media_id else None)
        log_status(f"Tweet posted successfully for {token_name}")
        return True
    except Exception as e:
        logger.error(f"Tweet failed: {e}")
        return False

def sales_processor():
    """Process sales from queue with proper intervals"""
    global processed_sales
    log_status("Starting sales processor thread")
    processed_sales = load_processed_sales()
    load_queue()
    
    while processing_active or not sales_queue.empty():
        try:
            log_status(f"Queue status: {sales_queue.qsize()} sales pending")
            sale = sales_queue.get(timeout=30)
            tx_version = sale.get("transactionVersion")
            
            if tx_version in processed_sales:
                log_status(f"Skipping duplicate sale: {tx_version}")
                continue
                
            if create_tweet(sale):
                save_queue()
            else:
                sales_queue.put(sale)
                save_queue()
                
            if not sales_queue.empty():
                log_status(f"Waiting {CONFIG['TWEET_INTERVAL']} seconds before next tweet...")
                time.sleep(CONFIG["TWEET_INTERVAL"])
                
        except queue.Empty:
            continue
        except Exception as e:
            logger.error(f"Processor error: {e}")
            time.sleep(5)

    log_status("Sales processor thread ending")

def check_for_new_sales():
    """Check for new sales and add to queue"""
    log_status(f"Bot monitoring started at {bot_start_time}")
    
    while processing_active:
        try:
            log_status("Initiating new sales check...")
            sales = fetch_recent_sales()
            new_sales = 0
            
            for sale in sorted(sales, key=lambda x: x["transactionTimestamp"]):
                tx_version = sale.get("transactionVersion")
                sale_time = datetime.fromisoformat(sale["transactionTimestamp"].replace("Z", "+00:00"))
                
                if (tx_version and 
                    tx_version not in processed_sales and 
                    sale_time > bot_start_time):
                    
                    with queue_lock:
                        sales_queue.put(sale)
                    new_sales += 1
            
            if new_sales:
                log_status(f"Found {new_sales} new sales - added to queue")
                save_queue()
            else:
                log_status("No new sales found this check")
            
            log_status(f"Next check in {CONFIG['CHECK_INTERVAL']} seconds...")
            time.sleep(CONFIG["CHECK_INTERVAL"])
            
        except Exception as e:
            logger.error(f"Checker error: {e}")
            time.sleep(60)

def shutdown_handler():
    """Handle graceful shutdown"""
    global processing_active
    log_status("Initiating shutdown sequence...")
    processing_active = False
    
    if not sales_queue.empty():
        save_queue()
    log_status("Shutdown complete - all systems off")

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    log_status("🚀 Starting NFT Sales Bot 🚀")
    
    try:
        processor_thread = Thread(target=sales_processor, daemon=True)
        processor_thread.start()
        check_for_new_sales()
        
    except KeyboardInterrupt:
        log_status("🛑 Received shutdown signal (Ctrl+C)")
        shutdown_handler()
    except Exception as e:
        log_status("💥 CRITICAL ERROR - Emergency shutdown")
        logger.error(f"Fatal error: {e}")
        shutdown_handler()
        exit(1)