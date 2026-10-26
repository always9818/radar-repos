# Radar de repos

Agente que monitorea repos top y en tendencia de GitHub (y menciones en HN, Reddit, Bluesky, X y TikTok),
los categoriza por funcionalidad y genera una descripción de uso con 2 ejemplos reales.

## Estado

- [x] **Fase 0** · Base del proyecto: configuración, esquema de base de datos y healthcheck
- [x] Fase 1 · Recolector de ranking de GitHub (primera corrida real en modo sin token: 22-oct-2026)
- [x] Fase 2 · Detector de tendencia (snapshots diarios + github.com/trending)
- [~] Fase 3 · Enriquecimiento con IA: listo y probado con respuestas simuladas; se activa al poner `ANTHROPIC_API_KEY`
- [x] Fase 4 · Fuentes sociales: Hacker News, Reddit (RSS) y Bluesky
- [x] Fase 5 · Panel web con "copiar URL" (con categoría provisional hasta la fase 3)
- [~] Fase 6 · Automatización y monitoreo: corrida diaria local activa; versión en la nube lista y probada
  localmente, falta activarla con `scripts/subir_a_github.ps1`
- [ ] Fase 7 · X y TikTok (opcional)

## Arranque

```bash
uv sync
cp .env.example .env   # y rellena GITHUB_TOKEN y ANTHROPIC_API_KEY
uv run radar-healthcheck
uv run pytest
```

## Loop de verificación de la fase 0

`radar-healthcheck` sale con código 1 si algo falla. Ninguna fase posterior debe correr si no está en verde.

| Chequeo | Qué valida |
|---|---|
| entorno | `GITHUB_TOKEN` y `ANTHROPIC_API_KEY` presentes |
| github | Token válido y límite de peticiones disponible (avisa bajo `GITHUB_MIN_REMAINING`) |
| anthropic | Clave válida y que el modelo de `ANTHROPIC_MODEL` exista |
| base_de_datos | Abre, aplica el esquema, tiene las 9 tablas y acepta escritura/lectura |

Las llamadas de red reintentan 3 veces con espera creciente (1 s, 2 s) ante errores 5xx o caídas de red.
Cada corrida queda registrada en la tabla `corridas`.

## Fase 1 · Ranking de GitHub

```bash
uv run radar-ranking                        # todas las consultas
uv run radar-ranking --consulta top_python  # solo una
uv run radar-ranking --sin-guardar          # buscar y validar sin escribir en la base
```

Las consultas se editan en [config/consultas.toml](config/consultas.toml) (sintaxis de búsqueda de GitHub,
con marcadores `{hace_7d}`, `{hace_30d}`, `{hace_90d}`).

Loop de verificación por consulta (hasta 3 intentos antes de marcarla como fallida):

| Chequeo | Qué valida |
|---|---|
| cantidad | Llegaron al menos `minimo` repos |
| duplicados | Ningún repo repetido en el lote |
| URL | `html_url` coincide con `https://github.com/{owner/nombre}` |
| estrellas | Ningún repo con 0 estrellas en un ranking |
| muestra | `muestra_urls` repos al azar existen de verdad en la API |

Límites: espera sola si el límite de GitHub se renueva en menos de 70 s; si no, corta y marca las consultas
restantes como no corridas. Reintenta errores 5xx y caídas de red con espera creciente.

Qué guarda: el repo (sin pisar `primera_vez`), una foto de estrellas por día en `snapshots` (base de la
fase 2) y una mención `github_ranking` por repo y consulta.

### Modo sin token

Si `GITHUB_TOKEN` está vacío, `radar-ranking` y `radar-tendencia` funcionan igual en modo anónimo
(10 búsquedas/min y 60 peticiones/hora): tardan más, verifican 1 repo al azar por consulta en lugar de 3
y refrescan menos repos por corrida. Sirve para ir acumulando historial mientras no hay token.

## Fase 2 · Tendencia

```bash
uv run radar-ranking      # primero: trae repos y la foto de estrellas de hoy
uv run radar-tendencia    # después: trending + refresco de faltantes + cálculo
uv run radar-tendencia --sin-trending --top 30
```

Cálculo por repo (umbrales en [config/tendencia.toml](config/tendencia.toml)):

- `delta_dia`: estrellas ganadas por día desde la foto anterior (si faltan días, se promedia).
- `base_diaria`: promedio diario de la semana previa.
- `score = delta_dia / max(base_diaria, piso_base)`. Un 3 significa "hoy ganó el triple de lo normal".

| Nivel | Regla |
|---|---|
| 🚀 explosivo | score ≥ 3 y al menos 100 estrellas/día |
| 📈 en_alza | score ≥ 1.5 y al menos 20/día; o, sin base todavía, al menos 100/día |
| 🌱 nuevo_rapido | creado hace ≤ 30 días y promedia ≥ 300 estrellas/día desde que nació |
| estable / sin_datos | el resto / solo hay una foto y no está en trending |

Confianza: alta con ≥ 7 fotos, media con ≥ 3 y baja con menos. Mientras no haya historial, los repos de
github.com/trending usan su "N stars today" como `delta_dia`.

Loop de verificación:

| Chequeo | Qué valida |
|---|---|
| trending | La página trae al menos `minimo_repos`, sin duplicados y con estrellas legibles (3 intentos) |
| refresco | Los repos seguidos sin foto de hoy se consultan por id; los borrados dejan de seguirse |
| cobertura | Al menos 90 % de los repos vistos en los últimos 7 días tienen foto de hoy (sin token es solo un aviso: con 60 peticiones/hora no alcanza para refrescar todos) |
| cálculo | Hay una tendencia por cada repo con foto de hoy |

Importante: el historial solo crece si `radar-ranking` y `radar-tendencia` corren **todos los días**.

## Fase 3 · Enriquecimiento con IA

```bash
uv run radar-ia                    # enriquece repos pendientes (también corre dentro de radar-diario)
uv run radar-ia --limite 10        # probar con pocos
uv run radar-ia --costo-max 0.50   # tope de gasto para esta corrida
```

Por cada repo, Claude escribe **en español**: la categoría (de la lista cerrada), qué es y para qué sirve, y
2 ejemplos concretos de uso en la vida real. El panel los muestra en lugar de la categoría provisional y del
cuadro "pendiente".

- **Se activa solo** al poner `ANTHROPIC_API_KEY` en `.env` (o como secreto en GitHub). Sin clave, el paso
  sale bien sin hacer nada.
- **Modelo:** `ANTHROPIC_MODEL` en `.env`. Configurado con `claude-haiku-5-5` para ahorrar (unas 40 veces más
  barato). Si falta la variable se usa `claude-opus-5-5`, la mejor calidad. Haiku 5.5 no tiene respaldo
  automático ante rechazos, así que con Haiku un repo rechazado simplemente se reintenta en la próxima corrida.
- **Orden:** primero los repos en tendencia, luego los mencionados en redes y después el resto por estrellas.
- **README** desde `raw.githubusercontent.com` (no gasta la API de GitHub), sin badges, imágenes ni HTML, y
  recortado a 6000 caracteres. Va delimitado como dato: el prompt le dice al modelo que ignore las
  instrucciones que trae el README.
- **Salida estructurada:** JSON con esquema fijo (la categoría solo puede ser una de la lista) y, en los modelos
  que lo admiten (Opus y Sonnet), respaldo automático (`fallbacks: "default"`) si el modelo rechaza un pedido.
- **Gasto controlado** ([config/ia.toml](config/ia.toml)): como máximo 150 repos y US$2 estimados por corrida;
  lo que quede sigue al día siguiente. Cada pedido son ~2000 tokens de entrada y algunos cientos de salida:
  unos US$0,02 por repo con Opus 5.5 (≈ US$18 para los ~900 repos actuales y ≈ US$1 por día para los nuevos),
  o unos US$0,0005 con Haiku 5.5. Son estimaciones; el gasto real se ve en la consola de Anthropic.

Loop de verificación por repo: categoría de la lista, los 3 textos en español con largos razonables y dos
ejemplos distintos. Si falla, reintenta una vez diciéndole al modelo qué corregir; si vuelve a fallar, queda
pendiente para la próxima corrida. Si fallan más del 20 % en una corrida, el paso se marca en rojo.

## Fase 4 · Hacker News

```bash
uv run radar-hackernews            # últimos 7 días (ver config/hackernews.toml)
uv run radar-hackernews --dias 30
```

Usa la API pública de Algolia (sin clave). Hace dos búsquedas: historias con link directo a GitHub
(≥ 10 puntos) y "Show HN" con el link en el texto (≥ 5 puntos). Los links se normalizan
(`/tree/...`, `/blob/...`, `.git` y entidades HTML como `&#x2F;`) y se ignoran páginas que no son repos
(`/features`, `/sponsors`, `gist.github.com`…).

Cada mención se guarda con los puntos de la historia como `interaccion`:

- Si el repo ya está en la base, va directo a `menciones`.
- Si no, queda en `menciones_pendientes` y se confirma en la API de GitHub (los de más puntos primero,
  hasta `max_resolver` por corrida; 20 sin token). Los que dan 404 se descartan y los renombrados se
  siguen por id. Al confirmarse, el repo entra con su foto del día y pasa a ser seguido por la fase 2.

Loop de verificación:

| Chequeo | Qué valida |
|---|---|
| API | Algolia responde (3 intentos con espera creciente) y trae al menos `minimo_historias` |
| estructura | Toda historia tiene título y fecha |
| sin pérdidas | Cada mención extraída quedó en `menciones`, en pendientes o se descartó por 404 |

Las piezas comunes (extraer links, registrar, resolver pendientes y verificar pérdidas) están en
`menciones.py` y las usan las tres redes.

## Fase 4 · Reddit y Bluesky

```bash
uv run radar-reddit     # ~2 min: 14 feeds con pausa entre cada uno
uv run radar-bluesky
```

Ajustes de ambos en [config/redes.toml](config/redes.toml).

**Reddit:** la API JSON responde 403 sin cuenta, así que se leen los feeds RSS públicos "top de la semana"
de la búsqueda `url:github.com` y de 13 subreddits. El RSS no trae votos: la interacción es la posición
inversa en el top. Reddit limita fuerte las peticiones anónimas, por eso hay una pausa de 8 s entre feeds
y, ante un 429, se espera lo que indique `retry-after` (o 45 s) antes de reintentar.

**Bluesky:** API pública `api.bsky.app` (el dominio `public.api.bsky.app` responde 403). Sin cuenta solo da
la primera página de resultados (el cursor responde 403), así que se hace una búsqueda "top" y otra "latest".
Los links se toman del texto, de las facets y del embed; la interacción es likes + reposts y se descartan
posts con menos de 5 likes.

| Chequeo | Reddit | Bluesky |
|---|---|---|
| Fuente responde | Cada feed: 3 intentos; 403/404 no se reintentan | 3 intentos; 403 no se reintenta |
| Formato | Debe ser Atom con entradas | Debe traer `posts` con texto y autor |
| Volumen | Al menos 70 % de los feeds leídos (un feed caído solo avisa) | Al menos `minimo_posts` |
| Duplicados | Mismo post en búsqueda y subreddit cuenta una vez | Mismo post en top y latest cuenta una vez |
| Sin pérdidas | Igual que Hacker News | Igual que Hacker News |

### Pendientes y modo sin token

Las tres redes comparten la tabla `menciones_pendientes`. Sin token, cada corrida confirma pocos repos
(HN 20, Reddit 10, Bluesky 10), así que la lista de espera crece más rápido de lo que se vacía.
Con `GITHUB_TOKEN` el tope sube a 100–200 por corrida y se vacía en uno o dos días.

## Fase 5 · Panel web

```bash
uv run radar-panel      # exporta la base a panel/data.js (valida antes de escribir)
```

Luego abre `panel/index.html` con doble clic (funciona sin servidor) o súbelo a cualquier hosting
estático junto con `data.js`.

Qué muestra:

- **Radar** animado: los repos en tendencia como puntos, un sector por categoría; más cerca del centro =
  más estrellas por día. Clic en un punto = filtra su tarjeta.
- **Vistas:** En tendencia · En redes · Top ranking · Todos. Filtros por categoría, lenguaje y búsqueda;
  orden por señal, estrellas, menciones en redes o antigüedad.
- **Tarjeta por repo:** nivel de tendencia y estrellas/día, categoría, descripción, los 2 ejemplos de uso
  (cuando exista la fase 3), links a las discusiones en HN/Reddit/Bluesky y botón **Copiar URL**.
- **Salud:** última corrida de cada recolector y si falló.
- Tema claro/oscuro (sigue al sistema; se puede cambiar) y diseño adaptado a celular.

Mientras no exista la fase 3, la categoría sale de palabras clave de los topics y la descripción
(`categoria_provisional` en `panel.py`) y se marca como *provisional*. Los repos con contenido para
adultos se marcan como sensibles y se ocultan por defecto.

Loop de verificación:

| Chequeo | Qué valida |
|---|---|
| exportación | Sin duplicados, cada URL es `https://github.com/owner/repo` y coincide con el nombre, categorías y niveles válidos, conteos que cuadran |
| seguridad | Los textos de GitHub y redes se escapan en el HTML; `</script>` no puede romper `data.js`; solo se enlazan URLs `https://` |
| navegador | Revisado en escritorio y celular (375 px), tema claro y oscuro, sin errores de consola ni scroll horizontal; copiar URL con clic real |

## Fase 6 · Corrida diaria

```bash
uv run radar-diario                     # todo en orden; no repite si hoy ya hubo una corrida completa
uv run radar-diario --forzar            # repetirla igual
uv run radar-diario --solo reddit panel # solo algunos pasos
```

Orden: ranking → hackernews → reddit → bluesky → tendencia → ia → panel (el panel traduce lo nuevo antes de
exportar). La tendencia va después de las redes para incluir los repos que ellas confirmaron ese día.
Una corrida típica sin token tarda ~3–10 min (Reddit es lo más lento por sus límites).

**Programada en Windows** todos los días a las 08:00 (tarea "Radar de repos - corrida diaria"):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\programar_tarea.ps1              # registrar / actualizar
powershell -ExecutionPolicy Bypass -File scripts\programar_tarea.ps1 -Hora 19:30  # cambiar la hora
powershell -ExecutionPolicy Bypass -File scripts\programar_tarea.ps1 -Quitar      # eliminarla
```

Corre con `pythonw.exe` (sin ventanas), solo con la sesión iniciada, también con batería, y si a esa hora la
PC estaba apagada la corre en cuanto vuelva a estar disponible. No despierta la PC si está suspendida.

Loop de verificación:

| Chequeo | Qué valida |
|---|---|
| por paso | Cada recolector corre en su propio proceso con tiempo máximo; si falla se reintenta una vez tras 60 s |
| aislamiento | Un paso caído no detiene los demás (un Reddit caído no deja el día sin ranking) |
| resultado | Al final hay fotos de estrellas de hoy y `panel/data.js` se regeneró en esta corrida |
| una sola vez | Candado contra corridas simultáneas; no se repite el día si ya hubo una corrida completa |
| registro | `logs/AAAA-MM-DD.log` con la salida completa (se guardan 30 días) y la tarea `diario` en `corridas`, visible en la franja de salud del panel |

### Chequeo de salud

```bash
uv run radar-salud      # integridad de la base, última corrida diaria y estado de cada fuente
```

Error (sale con código 1) si la base está dañada, si la última corrida diaria completa tiene más de 30 h o si
una fuente lleva 3 corridas seguidas fallando. Aviso si una fuente falló una vez, si quedó una corrida
colgada o si hay más de 1000 menciones esperando confirmación.

## Fase 6 · En la nube (GitHub Actions + GitHub Pages)

Con la nube el radar corre aunque la PC esté apagada, y el panel queda publicado en
`https://<usuario>.github.io/radar-repos/`.

**Activación (una sola vez, desde la PC):**

```powershell
gh auth login
powershell -ExecutionPolicy Bypass -File scripts\subir_a_github.ps1      # repo público (Pages gratis)
```

El script verifica herramientas y sesión, corre todas las pruebas (si alguna falla no sube nada), comprueba que
`.env`, la base y los modelos no se suban, crea el repo, sube la base actual a la rama `datos`, activa Pages,
lanza la primera corrida y quita la tarea local (con `-MantenerTareaLocal` la deja, pero la PC y la nube
guardarían bases distintas).

**Cómo funciona** ([.github/workflows/radar-diario.yml](.github/workflows/radar-diario.yml)), todos los días
a las 14:00 UTC (08:00 Guatemala) o a mano desde la pestaña Actions:

1. Recupera la base de la rama `datos` ([scripts/ci_datos.sh](scripts/ci_datos.sh)).
2. Usa el caché de modelos de traducción (los baja solo si cambió `traductor.toml`).
3. Corre `radar-diario` y `radar-salud`.
4. Guarda la base en la rama `datos`: un único commit comprimido que se reemplaza en cada corrida, así el
   repo no crece. Antes verifica integridad y que no se hayan perdido repos, fotos ni menciones; después
   comprueba que el remoto quedó con exactamente lo que se subió.
5. Sube logs y respaldo de la base como artefacto (30 días) y publica el panel en GitHub Pages.
6. Si la corrida o la salud fallaron, el job termina en rojo y GitHub avisa por correo.

No necesita secretos: el `GITHUB_TOKEN` de Actions da 1000 peticiones/hora a la API de GitHub (sin el modo
anónimo). `ANTHROPIC_API_KEY` se agrega como secreto cuando exista la fase 3.

Si Reddit bloquea los servidores de GitHub (pasa con IPs de centros de datos), crea la variable del repo
`RADAR_OMITIR=reddit` (Settings → Secrets and variables → Actions → Variables). Localmente:
`uv run radar-diario --omitir reddit`.

Probado localmente: el flujo pasa `actionlint`; los scripts de PowerShell no tienen errores de sintaxis; el
guardado en `datos` se probó contra un repo Git simulado (crear la rama, recuperarla en otra máquina y
reemplazarla con un solo commit, y rechazar una base que perdió datos sin tocar la buena). Lo que solo se
verá al activarlo: los permisos de Pages en tu cuenta y si Reddit responde desde GitHub.

## Traductor al español

Nada llega al panel en inglés: las descripciones de GitHub y los textos de HN/Reddit/Bluesky pasan por un
traductor **local** (modelos de Argos Translate ejecutados con CTranslate2: sin claves, sin enviar datos a
ningún servicio y sin PyTorch). Chino → inglés → español.

```bash
uv run radar-traducir --descargar   # la primera vez: baja los modelos (~90 MB en→es, ~80 MB zh→en)
uv run radar-traducir               # traduce lo nuevo (radar-panel también lo hace solo antes de exportar)
uv run radar-traducir --reintentar  # vuelve a intentar los que fallaron (p. ej. tras agregar un modelo)
```

Cada traducción se guarda en la tabla `traducciones` por el hash del texto original: nunca se traduce dos
veces, y si la descripción cambia en GitHub se traduce de nuevo. Primera corrida: ~900 textos en ~2 min.

Cómo evita los errores típicos de la traducción automática:

- **URLs, `código`, hashtags y emojis** se protegen y no pasan por el modelo.
- **Nombres propios** (glosario de [config/traductor.toml](config/traductor.toml) + el nombre del repo) se
  cambian por marcadores (`N1`, `N2`…) antes de traducir y se restauran después: así "Scrapy" no sale
  "Rastrillado" ni "Rust" sale "Óxido".
- **Títulos en Title Case** ("JavaScript Style Guide") el modelo los copia sin traducir; por eso cada frase
  se traduce en 5 variantes (original, con punto, en minúsculas, "Description: …", "It is a …") y se
  elige la que más suena a español sin perder nombres propios.
- **Correcciones** de traducciones literales conocidas ("Pitón" → "Python") en el mismo archivo de config.

Loop de verificación por texto: no vacío, sin tokens rotos (`⁇`), largo razonable, que no siga en inglés y
que no sea una copia del original; si falla, reintenta con más calidad (beam 4). Si vuelve a fallar, queda
sin traducción y el panel muestra "Descripción no disponible en español" (nunca el inglés). `radar-panel`
no escribe el panel si algún texto visible no pasó por el traductor.

Limitaciones: es traducción automática local, a veces literal ("Código visual del estudio"). Cuando haya
`ANTHROPIC_API_KEY`, la fase 3 escribirá en español la descripción de uso con mucha mejor calidad.
Japonés y coreano necesitan sus modelos (`ja_en`, `ko_en` en `traductor.toml`).

## Estructura

```
config/
  consultas.toml  # consultas del ranking
  tendencia.toml  # umbrales del detector de tendencia
  hackernews.toml # ventana, puntos mínimos y límites de Hacker News
  redes.toml      # subreddits, pausas, likes mínimos y límites de Reddit y Bluesky
  traductor.toml  # modelos, glosario de nombres propios y correcciones del traductor
  ia.toml         # esfuerzo, topes por corrida (repos y US$) y precios para estimar el gasto
src/radar/
  config.py       # carga .env
  db.py           # conexión SQLite y bitácora de corridas
  schema.sql      # repos, snapshots, menciones, categorias, enriquecimiento, corridas
  healthcheck.py  # loop de verificación de la fase 0
  github.py       # cliente de GitHub: búsqueda paginada, límites y reintentos
  ranking.py      # fase 1: recolectar, validar y guardar
  tendencia.py    # fase 2: trending, refresco, cálculo y clasificación
  menciones.py    # fase 4: extraer links, registrar menciones y resolver pendientes
  hackernews.py   # fase 4: recolector de Hacker News
  reddit.py       # fase 4: recolector de Reddit (RSS)
  bluesky.py      # fase 4: recolector de Bluesky
  panel.py        # fase 5: exporta la base a panel/data.js y categoría provisional
  traductor.py    # traductor local al español (detección de idioma, protección de nombres, verificación)
  diario.py       # fase 6: corrida diaria completa con reintentos, candado y log
  salud.py        # fase 6: chequeo de salud, comparación con la base anterior y respaldo consistente
  ia.py           # fase 3: categoría, descripción de uso y 2 ejemplos con Claude (salida estructurada)
scripts/
  programar_tarea.ps1  # registra/cambia/quita la tarea diaria en el Programador de tareas de Windows
  ci_datos.sh          # guarda/recupera la base en la rama datos (GitHub Actions)
  subir_a_github.ps1   # activa la nube: crea el repo, sube código y base, activa Pages
.github/workflows/
  radar-diario.yml     # corrida diaria en GitHub Actions + publicación del panel
logs/             # un log por día (no va a git)
modelos/          # modelos de traducción descargados (no van a git)
panel/
  index.html      # panel web estático (HTML + CSS + JS, sin dependencias)
  data.js         # datos generados por radar-panel
tests/            # pruebas con APIs simuladas (sin red)
  fixtures/       # fragmentos reales de trending, Reddit RSS y Bluesky para probar los lectores
```
