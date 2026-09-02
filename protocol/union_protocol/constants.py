"""Protocol-wide constants. Both the server and clients import these."""

PROTOCOL_VERSION = "1"

# Signed-request headers (see signing.py).
HEADER_NODE = "X-Union-Node"
HEADER_TS = "X-Union-Ts"
HEADER_NONCE = "X-Union-Nonce"
HEADER_SIG = "X-Union-Sig"

# The server rejects a signed request whose timestamp is further than this
# from its own clock, and any nonce it has seen from the same node within
# the retention window.
SIG_SKEW_SECONDS = 60
NONCE_RETENTION_SECONDS = 300

# HKDF info strings. Changing one is a protocol break.
HKDF_WRAP_INFO = b"union/wrap/v1"

# Limits.
MAX_MESSAGE_BYTES = 1_000_000          # ciphertext, per message
MAX_BLOB_BYTES = 50 * 1024 * 1024      # ciphertext, per attachment
MAX_RECIPIENTS = 25                    # per send
MAX_SENDS_PER_MINUTE = 30              # per sender
MAX_INBOX_QUEUE = 50                   # undelivered messages waiting on one recipient's stream

# Timing.
DELIVERY_WINDOW_SECONDS = 30           # server holds a message this long waiting for the ack
BLOB_WINDOW_SECONDS = 300              # staged attachment lifetime
PING_INTERVAL_SECONDS = 20             # server -> node keepalive on the event stream
OFFLINE_AFTER_SECONDS = 60             # no heartbeat for this long -> offline

MODES = ("data", "execute", "ask")
KINDS = ("data", "task", "reply")
STATUSES = ("busy", "idle", "offline")

# Which message kinds each mode accepts.
MODE_ACCEPTS = {
    "data": {"data", "reply"},
    "execute": {"data", "task", "reply"},
    "ask": {"data", "task", "reply"},
}

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$"
