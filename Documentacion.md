# Documentación

---

## Arquitectura general

El sistema sigue un modelo **productor–consumidor** con tres contenedores independientes que se comunican a través de Redis.

```
┌────────────────────────────────────────────────────────────┐
│                      docker compose                        │
│                                                            │
│  ┌─────────┐  ZINCRBY   ┌───────┐  ZREVRANGE  ┌──────────┐ │
│  │  Miner  │ ─────────► │ Redis │ ──────────► │Visualizer│ │
│  │(Python) │  palabras  │   7   │     SSE     │  (Node)  │ │
│  └─────────┘            └───────┘             └──────────┘ │
│       │                                             │      │
│  API GitHub                                     Navegador  │
└────────────────────────────────────────────────────────────┘
```

| Contenedor | Tecnología | Rol |
|---|---|---|
| `redis` | Redis 7 Alpine | Broker compartido, almacena conteos en sorted sets |
| `miner` | Python 3.12 | Productor: obtiene repos, extrae palabras, escribe en Redis |
| `visualizer` | Node.js 20 + Express | Consumidor: lee Redis, transmite datos al navegador vía SSE |

---

## Estructura de archivos

```
github-word-miner/
├── docker-compose.yml
├── .env.example
├── README.md
├── DOCUMENTACION.md
├── miner/
│   ├── miner.py           ← lógica principal del productor
│   ├── Dockerfile
│   └── requirements.txt
├── visualizer/
│   ├── server.js          ← servidor Express con SSE
│   ├── Dockerfile
│   ├── package.json
│   └── public/
│       └── index.html     ← dashboard (Chart.js + cliente SSE)
└── tests/
    └── test_miner.py      ← pruebas unitarias
```

---

## Componente Miner

### Flujo de ejecución

```
1. Conectar a Redis
2. Crear generadores para Python y Java (intercalados)
3. Por cada repositorio:
   a. Obtener lista de archivos (.py o .java)
   b. Descargar hasta MAX_FILES_PER_REPO archivos
   c. Extraer palabras de los nombres de funciones/métodos
   d. Publicar conteos en Redis
   e. Esperar SLEEP_BETWEEN_REPOS segundos
4. Al completar 10 páginas, reiniciar desde página 1
```

### Descubrimiento de repositorios

Consulta el endpoint de búsqueda de la API de GitHub ordenando por stars descendente:

```
GET https://api.github.com/search/repositories
    ?q=language:python stars:>500
    &sort=stars
    &order=desc
    &per_page=5
    &page=1
```

Esto garantiza que los repositorios se procesen en orden descendente de popularidad, comenzando por los más populares de todo GitHub.

### Extracción de palabras en Python

Utiliza el módulo `ast` de la biblioteca estándar para construir el árbol sintáctico del archivo y recorrer todos los nodos `FunctionDef` y `AsyncFunctionDef`:

```python
tree = ast.parse(source)
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        words.extend(split_identifier(node.name))
```

**Ventaja**: detección 100% precisa, maneja cualquier sintaxis válida de Python incluyendo funciones anidadas, decoradores y anotaciones de tipo.

### Extracción de palabras en Java

Utiliza una expresión regular que detecta el patrón de declaración de métodos:

```
[modificadores] <tipoRetorno> <nombreMétodo> (
```

Los nombres detectados se filtran contra un conjunto de palabras reservadas de Java (`if`, `for`, `while`, `class`, etc.) para evitar falsos positivos.

**Decisión de diseño**: se optó por regex en lugar de un parser completo (como `javalang`) para evitar dependencias pesadas y reducir el tiempo de inicio del contenedor. El regex cubre correctamente la gran mayoría de declaraciones reales de métodos Java.

### División de identificadores

La función `split_identifier` convierte un nombre de función en palabras individuales manejando todas las convenciones de nomenclatura:

| Entrada | Salida |
|---|---|
| `make_response` | `["make", "response"]` |
| `retainAll` | `["retain", "all"]` |
| `GetUserName` | `["get", "user", "name"]` |
| `getHTTPStatus` | `["get", "http", "status"]` |
| `__init__` | `["init"]` |
| `MAX_RETRY_COUNT` | `["max", "retry", "count"]` |

Algoritmo:
1. Eliminar guiones bajos al inicio y fin
2. Dividir por guiones bajos (snake_case)
3. Dentro de cada segmento, insertar límites de camelCase con regex
4. Convertir todo a minúsculas
5. Filtrar palabras con menos de `MIN_WORD_LENGTH` caracteres

### Publicación en Redis

Las palabras se publican en tres sorted sets usando `ZINCRBY` dentro de un pipeline atómico:

```python
pipe = r.pipeline()
for word in words:
    pipe.zincrby("word_counts:python", 1, word)  # conteo del lenguaje
    pipe.zincrby("word_counts:all",    1, word)  # conteo combinado
pipe.execute()
```

### Manejo del rate limit

Cuando GitHub responde con código 403, el miner lee el header `X-RateLimit-Reset` para saber exactamente cuántos segundos esperar antes de reintentar, evitando esperas innecesariamente largas o demasiado cortas.

---

## Componente Visualizer

### Endpoints

| Ruta | Tipo | Descripción |
|---|---|---|
| `GET /` | HTTP | Sirve el dashboard (`public/index.html`) |
| `GET /stream` | SSE | Transmite actualizaciones en tiempo real |
| `GET /api/words` | REST | Consulta puntual del ranking (JSON) |

### Parámetros de `/stream` y `/api/words`

| Parámetro | Valores | Por defecto |
|---|---|---|
| `language` | `all`, `python`, `java` | `all` |
| `top_n` | 1 – 100 | `20` |

### Formato del payload SSE

```json
{
  "language": "all",
  "words": [
    { "word": "get",  "count": 4821 },
    { "word": "set",  "count": 3904 }
  ],
  "meta": {
    "repos_processed": 42,
    "words_total": 198432,
    "python_total": 3102,
    "java_total": 2847
  }
}
```

### Por qué Server-Sent Events (SSE)

SSE es unidireccional (servidor → cliente), que es exactamente lo necesario para este caso. Funciona sobre HTTP/1.1 sin upgrade de protocolo, el navegador reconecta automáticamente si se cae la conexión, y no requiere librerías adicionales en el servidor.

### Por qué Node.js para el Visualizer

El event loop de Node.js maneja múltiples conexiones SSE simultáneas sin bloquear, sin necesidad de workers adicionales. A diferencia de Flask, no requiere configuración extra (gunicorn + gevent) para soportar conexiones de larga duración.

---

## Almacenamiento en Redis

| Clave | Tipo | Contenido |
|---|---|---|
| `word_counts:python` | Sorted Set | palabra → conteo (solo Python) |
| `word_counts:java` | Sorted Set | palabra → conteo (solo Java) |
| `word_counts:all` | Sorted Set | palabra → conteo (combinado) |
| `miner:meta` | Hash | `repos_processed`, `words_total` |

Se usan sorted sets porque permiten incrementar conteos en O(log N) con `ZINCRBY` y obtener el top-N en una sola operación con `ZREVRANGE`, lo que los hace ideales para un ranking en tiempo real.

Los datos persisten en el volumen `redis-data` gracias a la opción `--appendonly yes`, por lo que sobreviven reinicios del contenedor.

---

## Continuidad del proceso

El miner cicla por las páginas 1 a 10 de la búsqueda de GitHub (aproximadamente 50 repositorios por lenguaje por ciclo), intercalando Python y Java. Al agotar las 10 páginas, reinicia desde la página 1, permitiendo una ejecución continua indefinida.

Un conjunto en memoria (`procesados`) evita procesar el mismo repositorio dos veces dentro del mismo ciclo. Se limpia automáticamente al superar los 1.000 elementos para evitar consumo creciente de memoria.

---

## Pruebas

Las pruebas unitarias cubren las tres funciones de lógica pura del miner:

| Clase de prueba | Función probada | Casos |
|---|---|---|
| `TestSplitIdentifier` | `split_identifier` | 12 casos: snake, camel, pascal, dunders, acrónimos, mixtos |
| `TestExtractWordsPython` | `extract_words_python` | 8 casos: funciones simples, async, clases, anidadas, errores |
| `TestExtractWordsJava` | `extract_words_java` | 7 casos: públicos, privados, estáticos, throws, keywords |

Ejecutar con:

```bash
pip install pytest
pytest tests/test_miner.py -v
```

---

## Decisiones de diseño

**Redis sobre una base de datos relacional** — Los sorted sets de Redis ofrecen la operación `ZINCRBY` que incrementa atómicamente un contador y mantiene el orden automáticamente. Una base de datos SQL requeriría un `UPDATE ... SET count = count + 1` con locking explícito para cada palabra.

**AST para Python, regex para Java** — Python tiene un parser oficial en su biblioteca estándar que es gratuito en dependencias. Para Java, un parser completo como `javalang` agrega varios segundos al tiempo de inicio del contenedor y usa más memoria, sin una mejora significativa en los resultados para código bien formateado de repositorios populares.

**Tres sorted sets separados** — Mantener `word_counts:python`, `word_counts:java` y `word_counts:all` por separado permite filtrar por lenguaje en el dashboard sin recalcular nada. El costo es duplicar cada escritura, que es negligible comparado con el costo de recalcular el top-N dinámicamente.

**SSE sobre WebSockets** — El flujo de datos es estrictamente unidireccional: el servidor empuja actualizaciones, el cliente solo recibe. SSE es la tecnología diseñada exactamente para este patrón y es más simple de implementar y mantener.
