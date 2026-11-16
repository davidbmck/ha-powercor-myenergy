"""Integration settings."""

from datetime import timedelta

DOMAIN = "powercor_myenergy"
CONF_IMAP = "imap"
CONF_AUTOMATIC = "automatic_login"
CONF_SESSION = "session"
CONF_LAST_LOGIN = "last_login_attempt"
UPDATE_INTERVAL = timedelta(minutes=80)
HISTORY_INTERVAL = timedelta(hours=6)
LOGIN_COOLDOWN = timedelta(minutes=15)
FIRST_SYNC_DAYS = 30
RECENT_SYNC_DAYS = 14
MAX_SYNC_DAYS = 30
CONF_LAST_SYNC = "last_successful_sync"
CONF_RETRY_START = "incomplete_period_start"
