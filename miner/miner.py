import ast
import base64
import logging
import os
import re
import time
from typing import Generator
 
import redis
import requests
 
# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("miner")
 
# ── Configuration (env-overridable) ──────────────────────────────────────────
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379")
MAX_FILES_PER_REPO: int = int(os.getenv("MAX_FILES_PER_REPO", "30"))
REPOS_PER_PAGE: int = int(os.getenv("REPOS_PER_PAGE", "5"))
SLEEP_BETWEEN_FILES: float = float(os.getenv("SLEEP_BETWEEN_FILES", "0.3"))
SLEEP_BETWEEN_REPOS: float = float(os.getenv("SLEEP_BETWEEN_REPOS", "2.0"))
MIN_WORD_LENGTH: int = int(os.getenv("MIN_WORD_LENGTH", "2"))
 
# ── Constants ─────────────────────────────────────────────────────────────────
REDIS_KEYS = {
    "python": "word_counts:python",
    "java": "word_counts:java",
    "all": "word_counts:all",
}
# Metadata key: total repos/files processed
META_KEY = "miner:meta"
 
JAVA_KEYWORDS = frozenset(
    {
        "if", "for", "while", "switch", "catch", "try", "else", "do",
        "class", "interface", "enum", "new", "return", "import", "package",
        "extends", "implements", "super", "this", "throw", "throws",
        "abstract", "final", "static", "public", "private", "protected",
        "synchronized", "native", "strictfp", "void", "boolean", "int",
        "long", "double", "float", "short", "byte", "char",
    }
)
 
 
# ══════════════════════════════════════════════════════════════════════════════
#  Word splitting
# ══════════════════════════════════════════════════════════════════════════════
 
def split_identifier(name: str) -> list[str]:
    """
    Split a function/method identifier into constituent words.
 
    Handles:
      - snake_case  → split on underscores
      - camelCase   → split on case boundaries
      - PascalCase  → same
      - ALL_CAPS    → treated as single word per segment
      - Mixed       → snake + camel combined (e.g. get_HTTPResponse)
 
    Examples:
        make_response     → ["make", "response"]
        retainAll         → ["retain", "all"]
        getHTTPStatus     → ["get", "http", "status"]
        __init__          → ["init"]
    """
    # Strip leading/trailing underscores (Python dunders, private markers)
    name = name.strip("_")
    if not name:
        return []
 
    # First split on underscores (snake_case)
    snake_parts: list[str] = [p for p in name.split("_") if p]
 
    words: list[str] = []
    for part in snake_parts:
        # Then split camelCase within each segment:
        # Insert boundary before sequences like: lowercase→Uppercase, Uppercase→UppercaseLower
        camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", part)
        camel_split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", camel_split)
        words.extend(camel_split.lower().split("_"))
 
    return [w for w in words if len(w) >= MIN_WORD_LENGTH and w.isalpha()]
 
 
# ══════════════════════════════════════════════════════════════════════════════
#  Source-code parsers
# ══════════════════════════════════════════════════════════════════════════════
 
def extract_words_python(source: str) -> list[str]:
    """Parse Python source with the built-in `ast` module and collect all
    function/method names (FunctionDef and AsyncFunctionDef nodes)."""
    words: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return words
 
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            words.extend(split_identifier(node.name))
 
    return words
 
 
# Java method declaration regex.
# Matches: [modifiers] <returnType> <methodName> ( ... ) [throws ...] {
# Group 1 → method name
_JAVA_METHOD_RE = re.compile(
    r"""
    \b
    (?:(?:public|private|protected|static|final|abstract|
          synchronized|native|strictfp|default|transient|volatile)
       \s+)*                              # zero or more modifiers
    (?:[\w<>\[\],\s?]+?\s+)?              # return type (optional, non-greedy)
    (\w+)                                  # ← method name (group 1)
    \s*\(                                  # opening parenthesis
    """,
    re.VERBOSE,
)
 
 
def extract_words_java(source: str) -> list[str]:
    """Extract method names from Java source using a regex heuristic.
 
    A full Java parser (e.g. javalang) is more accurate but adds a heavy
    dependency and slows startup; this regex handles the overwhelming majority
    of real-world cases without false positives for keywords.
    """
    words: list[str] = []
    for match in _JAVA_METHOD_RE.finditer(source):
        name = match.group(1)
        if name in JAVA_KEYWORDS:
            continue
        words.extend(split_identifier(name))
    return words
 
 
# ══════════════════════════════════════════════════════════════════════════════
#  GitHub API helpers
# ══════════════════════════════════════════════════════════════════════════════
 
def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"token {GITHUB_TOKEN}"
    return h
 
 
def _gh_get(url: str, params: dict | None = None) -> requests.Response | None:
    """GET a GitHub API URL, handling rate-limit back-off automatically."""
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=_gh_headers(), params=params, timeout=15)
        except requests.RequestException as exc:
            log.warning("Network error (%s). Retry %d/3", exc, attempt + 1)
            time.sleep(5 * (attempt + 1))
            continue
 
        if resp.status_code == 403:
            reset_ts = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = max(reset_ts - time.time(), 5)
            log.warning("Rate-limited by GitHub. Sleeping %.0fs …", wait)
            time.sleep(wait)
            continue
 
        if resp.status_code == 404:
            return None
 
        if resp.status_code != 200:
            log.warning("GitHub returned %d for %s", resp.status_code, url)
            return None
 
        return resp
 
    return None
 
 
def iter_repos(language: str) -> Generator[dict, None, None]:
    """Yield repository metadata dicts, sorted by stars descending.
 
    Cycles through pages 1-10 (≈500 repos per language) then restarts so
    the miner runs continuously.
    """
    page = 1
    while True:
        log.info("Fetching page %d of %s repos …", page, language)
        resp = _gh_get(
            "https://api.github.com/search/repositories",
            params={
                "q": f"language:{language} stars:>500",
                "sort": "stars",
                "order": "desc",
                "per_page": REPOS_PER_PAGE,
                "page": page,
            },
        )
        if resp is None:
            time.sleep(10)
            continue
 
        items = resp.json().get("items", [])
        if not items:
            log.info("No more %s repos on page %d. Restarting …", language, page)
            page = 1
            continue
 
        yield from items
        page = (page % 10) + 1  # pages 1–10, then wrap
 
 
def get_file_paths(owner: str, repo: str, extension: str) -> list[str]:
    """Return all file paths in a repo matching *extension* (e.g. '.py')."""
    resp = _gh_get(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD",
        params={"recursive": "1"},
    )
    if resp is None:
        return []
 
    tree = resp.json().get("tree", [])
    return [
        item["path"]
        for item in tree
        if item.get("type") == "blob" and item["path"].endswith(extension)
    ]
 
 
def get_file_content(owner: str, repo: str, path: str) -> str | None:
    """Fetch and decode (base64) the content of a single file."""
    resp = _gh_get(f"https://api.github.com/repos/{owner}/{repo}/contents/{path}")
    if resp is None:
        return None
    data = resp.json()
    if isinstance(data, dict) and data.get("encoding") == "base64":
        raw = data.get("content", "")
        return base64.b64decode(raw).decode("utf-8", errors="ignore")
    return None
 
 
# ══════════════════════════════════════════════════════════════════════════════
#  Redis publisher
# ══════════════════════════════════════════════════════════════════════════════
 
def connect_redis() -> redis.Redis:
    client = redis.from_url(REDIS_URL, decode_responses=True)
    for i in range(10):
        try:
            client.ping()
            log.info("Connected to Redis at %s", REDIS_URL)
            return client
        except redis.ConnectionError:
            log.warning("Redis not ready. Retry %d/10 …", i + 1)
            time.sleep(3)
    raise RuntimeError("Could not connect to Redis")
 
 
def publish_words(r: redis.Redis, language: str, words: list[str]) -> None:
    """Atomically increment word counts in both the language-specific and
    combined Redis sorted sets."""
    if not words:
        return
    pipe = r.pipeline()
    for word in words:
        pipe.zincrby(REDIS_KEYS[language], 1, word)
        pipe.zincrby(REDIS_KEYS["all"], 1, word)
    pipe.execute()
 
 
# ══════════════════════════════════════════════════════════════════════════════
#  Main processing loop
# ══════════════════════════════════════════════════════════════════════════════
 
LANGUAGE_CONFIG = {
    "python": {"extension": ".py", "extractor": extract_words_python},
    "java": {"extension": ".java", "extractor": extract_words_java},
}
 
 
def process_repo(r: redis.Redis, owner: str, repo_name: str, language: str) -> int:
    """Process a single repository; return total words published."""
    cfg = LANGUAGE_CONFIG[language]
    log.info("  ▶ %s/%s  [%s]", owner, repo_name, language)
 
    paths = get_file_paths(owner, repo_name, cfg["extension"])
    if not paths:
        log.info("    No %s files found.", cfg["extension"])
        return 0
 
    # Limit files per repo to stay within API rate limits
    paths = paths[:MAX_FILES_PER_REPO]
    log.info("    %d file(s) to process …", len(paths))
 
    total_words = 0
    for fpath in paths:
        content = get_file_content(owner, repo_name, fpath)
        if content is None:
            continue
 
        words = cfg["extractor"](content)
        publish_words(r, language, words)
        total_words += len(words)
 
        time.sleep(SLEEP_BETWEEN_FILES)
 
    log.info("    ✓ %d words extracted.", total_words)
    return total_words
 
 
def main() -> None:
    r = connect_redis()
 
    # Track processed repos in memory to avoid duplicate processing in one pass
    processed: set[tuple[str, str]] = set()
    stats = {"repos": 0, "words": 0}
 
    # Interleave Python and Java repositories
    python_gen = iter_repos("python")
    java_gen = iter_repos("java")
 
    log.info("Miner started. Processing repositories continuously …")
 
    while True:
        for language, gen in [("python", python_gen), ("java", java_gen)]:
            repo = next(gen)
            full_name: str = repo["full_name"]
            key = (full_name, language)
 
            if key in processed:
                log.info("  (skip) Already processed %s [%s]", full_name, language)
                continue
 
            owner, repo_name = full_name.split("/", 1)
            try:
                words = process_repo(r, owner, repo_name, language)
                processed.add(key)
                stats["repos"] += 1
                stats["words"] += words
 
                # Store live stats so the visualizer can display them
                r.hset(
                    META_KEY,
                    mapping={
                        "repos_processed": stats["repos"],
                        "words_total": stats["words"],
                    },
                )
            except Exception as exc:
                log.error("Error processing %s: %s", full_name, exc)
 
            time.sleep(SLEEP_BETWEEN_REPOS)
 
        # Reset processed set every 1 000 repos to allow re-processing
        if len(processed) > 1_000:
            processed.clear()
 
 
if __name__ == "__main__":
    main()
 