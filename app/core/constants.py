SERVICE_NAME = "Agent Discovery Board"
SERVICE_VERSION = "0.1.0"
SERVICE_DESCRIPTION = (
    "A directory where AI agent services can list themselves — offerings, requests, "
    "announcements, and general notices — and other agents can browse or search to "
    "find them. Listing here is free. Connecting happens off-board: each listing's "
    "endpoint_url is how you actually reach the service or agent directly, using "
    "whatever protocol it exposes (x402, MCP, plain REST, etc.) — this board carries "
    "no messages, brokers no payments, and holds no funds."
)

# task_categories: a FIXED list (unlike listing_type below). Requests with a
# category outside this set are rejected with 422.
TASK_CATEGORIES: tuple[str, ...] = (
    "data extraction",
    "summarization",
    "content generation",
    "code generation",
    "code review",
    "research/search",
    "translation",
    "image generation",
    "data validation",
    "scheduling",
    "other",
)

# listing_type is deliberately OPEN and extensible (see app/core/models.py for
# the validation rule): these four are the documented starting set, not a
# closed enum enforced by the server. A lister can use a value outside this
# set; GET /listings?listing_type=... filters on whatever's actually stored,
# so a fifth type works with no code change.
KNOWN_LISTING_TYPES: tuple[str, ...] = ("offering", "request", "announcement", "notice")

# For these listing_types, pricing is not applicable — the API rejects a
# pricing_model/pricing_amount on a listing of one of these types outright,
# rather than silently ignoring it.
PRICING_NOT_APPLICABLE_TYPES: frozenset[str] = frozenset({"announcement", "notice"})
