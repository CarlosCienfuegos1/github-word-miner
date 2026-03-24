"""
Miner de Nombres de Funciones en GitHub
----------------------------------------
Obtiene código fuente Python y Java desde los repositorios más populares de GitHub,
extrae palabras desde los nombres de funciones y métodos (respetando camelCase y snake_case),
y publica los conteos de palabras en sorted sets de Redis.

Arquitectura: Lado productor del pipeline productor–consumidor.
              Las palabras se escriben en sorted sets de Redis:
                  word_counts:python  – conteos solo de Python
                  word_counts:java    – conteos solo de Java
                  word_counts:all     – conteos combinados
"""

import ast
import base64
import logging
import os
import re
import time
from typing import Generator

import redis
import requests

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("miner")

# ── Configuración (sobreescribible por variables de entorno) ──────────────────
GITHUB_TOKEN: str          = os.getenv("GITHUB_TOKEN", "")
REDIS_URL: str             = os.getenv("REDIS_URL", "redis://redis:6379")
MAX_FILES_PER_REPO: int    = int(os.getenv("MAX_FILES_PER_REPO", "30"))
REPOS_PER_PAGE: int        = int(os.getenv("REPOS_PER_PAGE", "5"))
SLEEP_BETWEEN_FILES: float = float(os.getenv("SLEEP_BETWEEN_FILES", "0.3"))
SLEEP_BETWEEN_REPOS: float = float(os.getenv("SLEEP_BETWEEN_REPOS", "2.0"))
MIN_WORD_LENGTH: int       = int(os.getenv("MIN_WORD_LENGTH", "2"))

# ── Constantes ────────────────────────────────────────────────────────────────
REDIS_KEYS = {
    "python": "word_counts:python",
    "java":   "word_counts:java",
    "all":    "word_counts:all",
}
# Clave de metadatos: total de repos y archivos procesados
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
#  División de identificadores
# ══════════════════════════════════════════════════════════════════════════════

def split_identifier(name: str) -> list[str]:
    """
    Divide un identificador de función/método en sus palabras componentes.

    Maneja:
      - snake_case  → divide por guiones bajos
      - camelCase   → divide por límites de mayúsculas/minúsculas
      - PascalCase  → igual que camelCase
      - ALL_CAPS    → cada segmento se trata como una palabra
      - Mixto       → snake + camel combinados (ej: get_HTTPResponse)

    Ejemplos:
        make_response  → ["make", "response"]
        retainAll      → ["retain", "all"]
        getHTTPStatus  → ["get", "http", "status"]
        __init__       → ["init"]
    """
    # Eliminar guiones bajos al inicio/fin (dunders de Python, marcadores privados)
    name = name.strip("_")
    if not name:
        return []

    # Primero dividir por guiones bajos (snake_case)
    snake_parts: list[str] = [p for p in name.split("_") if p]

    words: list[str] = []
    for part in snake_parts:
        # Luego dividir camelCase dentro de cada segmento:
        # Insertar límite antes de: minúscula→Mayúscula y MAYÚSCULAS→MayúsculaMinúscula
        camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", part)
        camel_split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", camel_split)
        words.extend(camel_split.lower().split("_"))

    return [w for w in words if len(w) >= MIN_WORD_LENGTH and w.isalpha()]


# ══════════════════════════════════════════════════════════════════════════════
#  Parsers de código fuente
# ══════════════════════════════════════════════════════════════════════════════

def extract_words_python(source: str) -> list[str]:
    """Parsea código Python con el módulo `ast` incorporado y recolecta todos
    los nombres de funciones y métodos (nodos FunctionDef y AsyncFunctionDef)."""
    words: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return words

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            words.extend(split_identifier(node.name))

    return words


# Regex para detectar declaraciones de métodos en Java.
# Coincide con: [modificadores] <tipoRetorno> <nombreMétodo> ( ... ) [throws ...] {
# Grupo 1 → nombre del método
_JAVA_METHOD_RE = re.compile(
    r"""
    \b
    (?:(?:public|private|protected|static|final|abstract|
          synchronized|native|strictfp|default|transient|volatile)
       \s+)*                              # cero o más modificadores
    (?:[\w<>\[\],\s?]+?\s+)?              # tipo de retorno (opcional, no codicioso)
    (\w+)                                  # ← nombre del método (grupo 1)
    \s*\(                                  # paréntesis de apertura
    """,
    re.VERBOSE,
)


def extract_words_java(source: str) -> list[str]:
    """Extrae nombres de métodos de código Java usando una heurística de regex.

    Un parser completo de Java (ej: javalang) sería más preciso pero agrega
    una dependencia pesada y ralentiza el inicio; este regex maneja la gran
    mayoría de casos reales sin falsos positivos por palabras reservadas.
    """
    words: list[str] = []
    for match in _JAVA_METHOD_RE.finditer(source):
        name = match.group(1)
        if name in JAVA_KEYWORDS:
            continue
        words.extend(split_identifier(name))
    return words


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers para la API de GitHub
# ══════════════════════════════════════════════════════════════════════════════

def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"token {GITHUB_TOKEN}"
    return h


def _gh_get(url: str, params: dict | None = None) -> requests.Response | None:
    """Realiza un GET a la API de GitHub manejando automáticamente el rate limit."""
    for intento in range(3):
        try:
            resp = requests.get(url, headers=_gh_headers(), params=params, timeout=15)
        except requests.RequestException as exc:
            log.warning("Error de red (%s). Reintento %d/3", exc, intento + 1)
            time.sleep(5 * (intento + 1))
            continue

        if resp.status_code == 403:
            reset_ts = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            espera = max(reset_ts - time.time(), 5)
            log.warning("Rate limit alcanzado. Esperando %.0fs …", espera)
            time.sleep(espera)
            continue

        if resp.status_code == 404:
            return None

        if resp.status_code != 200:
            log.warning("GitHub retornó %d para %s", resp.status_code, url)
            return None

        return resp

    return None


def iter_repos(language: str) -> Generator[dict, None, None]:
    """Genera diccionarios de metadatos de repositorios, ordenados por stars descendente.

    Cicla por las páginas 1-10 (aprox. 500 repos por lenguaje) y luego reinicia
    para que el miner corra de forma continua.
    """
    page = 1
    while True:
        log.info("Obteniendo página %d de repos %s …", page, language)
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
            log.info("No hay más repos %s en página %d. Reiniciando …", language, page)
            page = 1
            continue

        yield from items
        page = (page % 10) + 1  # páginas 1–10, luego vuelve a 1


def get_file_paths(owner: str, repo: str, extension: str) -> list[str]:
    """Retorna todas las rutas de archivos en un repo que coincidan con *extension* (ej: '.py')."""
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
    """Obtiene y decodifica (base64) el contenido de un archivo."""
    resp = _gh_get(f"https://api.github.com/repos/{owner}/{repo}/contents/{path}")
    if resp is None:
        return None
    data = resp.json()
    if isinstance(data, dict) and data.get("encoding") == "base64":
        raw = data.get("content", "")
        return base64.b64decode(raw).decode("utf-8", errors="ignore")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  Publicador en Redis
# ══════════════════════════════════════════════════════════════════════════════

def connect_redis() -> redis.Redis:
    client = redis.from_url(REDIS_URL, decode_responses=True)
    for i in range(10):
        try:
            client.ping()
            log.info("Conectado a Redis en %s", REDIS_URL)
            return client
        except redis.ConnectionError:
            log.warning("Redis no disponible. Reintento %d/10 …", i + 1)
            time.sleep(3)
    raise RuntimeError("No se pudo conectar a Redis")


def publish_words(r: redis.Redis, language: str, words: list[str]) -> None:
    """Incrementa atómicamente los conteos de palabras en los sorted sets
    específicos del lenguaje y en el combinado."""
    if not words:
        return
    pipe = r.pipeline()
    for word in words:
        pipe.zincrby(REDIS_KEYS[language], 1, word)
        pipe.zincrby(REDIS_KEYS["all"], 1, word)
    pipe.execute()


# ══════════════════════════════════════════════════════════════════════════════
#  Bucle principal de procesamiento
# ══════════════════════════════════════════════════════════════════════════════

LANGUAGE_CONFIG = {
    "python": {"extension": ".py",   "extractor": extract_words_python},
    "java":   {"extension": ".java", "extractor": extract_words_java},
}


def process_repo(r: redis.Redis, owner: str, repo_name: str, language: str) -> int:
    """Procesa un repositorio completo; retorna el total de palabras publicadas."""
    cfg = LANGUAGE_CONFIG[language]
    log.info("  ▶ %s/%s  [%s]", owner, repo_name, language)

    paths = get_file_paths(owner, repo_name, cfg["extension"])
    if not paths:
        log.info("    No se encontraron archivos %s.", cfg["extension"])
        return 0

    # Limitar archivos por repo para no exceder el rate limit de la API
    paths = paths[:MAX_FILES_PER_REPO]
    log.info("    %d archivo(s) a procesar …", len(paths))

    total_words = 0
    for fpath in paths:
        content = get_file_content(owner, repo_name, fpath)
        if content is None:
            continue

        words = cfg["extractor"](content)
        publish_words(r, language, words)
        total_words += len(words)

        time.sleep(SLEEP_BETWEEN_FILES)

    log.info("    ✓ %d palabras extraídas.", total_words)
    return total_words


def main() -> None:
    r = connect_redis()

    # Registro en memoria de repos ya procesados para evitar duplicados en un ciclo
    procesados: set[tuple[str, str]] = set()
    stats = {"repos": 0, "words": 0}

    # Intercalar repositorios Python y Java
    python_gen = iter_repos("python")
    java_gen   = iter_repos("java")

    log.info("Miner iniciado. Procesando repositorios de forma continua …")

    while True:
        for language, gen in [("python", python_gen), ("java", java_gen)]:
            repo = next(gen)
            full_name: str = repo["full_name"]
            key = (full_name, language)

            if key in procesados:
                log.info("  (omitir) Ya procesado %s [%s]", full_name, language)
                continue

            owner, repo_name = full_name.split("/", 1)
            try:
                words = process_repo(r, owner, repo_name, language)
                procesados.add(key)
                stats["repos"] += 1
                stats["words"] += words

                # Guardar estadísticas en vivo para que el visualizer las muestre
                r.hset(
                    META_KEY,
                    mapping={
                        "repos_processed": stats["repos"],
                        "words_total":     stats["words"],
                    },
                )
            except Exception as exc:
                log.error("Error procesando %s: %s", full_name, exc)

            time.sleep(SLEEP_BETWEEN_REPOS)

        # Limpiar el set cada 1.000 repos para evitar fugas de memoria
        if len(procesados) > 1_000:
            procesados.clear()


if __name__ == "__main__":
    main()