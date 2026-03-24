# Miner de Palabras de GitHub

Herramienta para identificar las palabras más utilizadas en nombres de funciones y métodos de código Python y Java, extrayendo datos en tiempo real desde los repositorios más populares de GitHub.

---

## Requisitos

- [Docker Desktop](https://www.docker.com/products/docker-desktop) instalado y corriendo

---

## Instalación

```bash
# Clonar el repositorio
git clone https://github.com/TU_USUARIO/github-word-miner
cd github-word-miner
```

---

## Ejecución

```bash
docker compose up --build
```

Abrir el dashboard en **http://localhost:5000**

Los primeros datos aparecen en aproximadamente 30 segundos.

---

## Uso del dashboard

- **Tabs superiores** — filtrar por todos los lenguajes, solo Python o solo Java
- **Slider top** — ajustar cuántas palabras muestra el ranking (5 a 50)
- **Indicador verde** — confirma que el dashboard está recibiendo datos en vivo

---

## Detener el sistema

```bash
# Detener los contenedores (los datos en Redis se conservan)
docker compose down

# Detener y eliminar todos los datos
docker compose down -v
```

---
---

# Configuración opcional

---

## Ramas disponibles

| Rama | Descripción |
|---|---|
| `main` | Configuración por defecto, funciona sin token (más lento) |
| `dev` | Incluye una configuración personalizada para usar tokens, procesa repositorios más rápido |

Para usar la rama `dev`:

```bash
git checkout dev
```

---

## Obtener y usar un token de GitHub

1. Ir a **https://github.com/settings/tokens**
2. Clic en **Generate new token (classic)**
3. Darle un nombre (ej: `word-miner`)
4. No marcar ningún scope — alcanza para repos públicos
5. Clic en **Generate token** al fondo
6. Copiar el token generado (empieza con `ghp_`) en el `.env`

---

## Nota sobre el archivo `.env`

Generalmente el archivo `.env` no se sube a un repositorio público porque puede contener información sensible como tokens o contraseñas. En este caso se hizo una excepción porque el proyecto es de carácter académico y el `.env` incluido está configurado para funcionar **sin token**, por lo que no expone ningún dato sensible. Si se desea usar un token propio, simplemente se edita el `.env` antes de ejecutar.
