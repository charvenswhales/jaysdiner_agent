import os
from dotenv import load_dotenv

load_dotenv()

# --- OpenRouter (OpenAI-compatible endpoint) ---
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Model used for the browsing sub-agent. Keep this cheap, it's called
# repeatedly per step while the agent reads the page and decides actions.
BROWSER_AGENT_MODEL = os.environ.get("BROWSER_AGENT_MODEL", "openai/gpt-4o-mini")

# --- Site ---
SITE_URL = "https://jaysdinerng.com"

# --- Browser runtime ---
# Defaults to headless=True, since a real server has no display and a
# headed browser will just crash there. Set BROWSER_HEADLESS=false locally
# if you want to watch the agent work in a visible browser window.
HEADLESS = os.environ.get("BROWSER_HEADLESS", "true").lower() == "true"

# --- Auth ---
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "dev-only-insecure-key-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "10080"))  # 7 days

if JWT_SECRET_KEY == "dev-only-insecure-key-change-me":
    print(
        "WARNING: JWT_SECRET_KEY is not set. Using an insecure default. "
        "Anyone can forge login tokens. Set a real JWT_SECRET_KEY before "
        "this runs anywhere other than your own machine.",
        flush=True,
    )

# --- Monei Connect ---
MONEI_CLIENT_ID = os.environ.get("MONEI_CLIENT_ID", "")
MONEI_CLIENT_SECRET = os.environ.get("MONEI_CLIENT_SECRET", "")
MONEI_REDIRECT_URI = os.environ.get("MONEI_REDIRECT_URI", "http://localhost:8000/connect/monei/callback")
# Where the browser lands back in OUR app after the callback finishes
MONEI_FRONTEND_RETURN_URL = os.environ.get("MONEI_FRONTEND_RETURN_URL", "http://localhost:3000/profile")
MONEI_AUTHORIZE_BASE = "https://monei.cc/connect/authorize"
MONEI_API_BASE = "https://api.monei.cc"
MONEI_SCOPES = "wallet:read wallet:withdraw"