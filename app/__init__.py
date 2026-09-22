from dotenv import load_dotenv

# Loaded as a side effect of importing the app package, before any submodule
# runs — so every module can read required env vars at import time (the
# fail-loudly pattern used throughout this service) without each one having
# to load .env itself.
load_dotenv()
