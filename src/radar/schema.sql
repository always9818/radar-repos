-- Esquema del radar. Idempotente: se puede ejecutar en cada arranque.
-- SQL compatible en lo posible con Postgres para migrar a Supabase después.

-- Un repo por fila; la clave es el id numérico de GitHub (no cambia si renombran el repo).
CREATE TABLE IF NOT EXISTS repos (
    id              INTEGER PRIMARY KEY,
    full_name       TEXT NOT NULL UNIQUE,          -- "owner/nombre"
    url             TEXT NOT NULL,                 -- https://github.com/owner/nombre
    descripcion_gh  TEXT,                          -- descripción original de GitHub
    lenguaje        TEXT,
    topics          TEXT NOT NULL DEFAULT '[]',    -- lista JSON
    estrellas       INTEGER NOT NULL DEFAULT 0,
    forks           INTEGER NOT NULL DEFAULT 0,
    es_fork         INTEGER NOT NULL DEFAULT 0,
    archivado       INTEGER NOT NULL DEFAULT 0,
    creado_gh       TEXT,                          -- fecha de creación en GitHub (ISO 8601)
    ultimo_push_gh  TEXT,
    primera_vez     TEXT NOT NULL,                 -- cuándo lo vio el radar por primera vez
    ultima_vez      TEXT NOT NULL
);

-- Foto diaria de estrellas: de aquí sale la velocidad y aceleración (fase 2).
CREATE TABLE IF NOT EXISTS snapshots (
    repo_id    INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    fecha      TEXT NOT NULL,                      -- YYYY-MM-DD
    estrellas  INTEGER NOT NULL,
    forks      INTEGER NOT NULL,
    PRIMARY KEY (repo_id, fecha)
);

-- Dónde apareció cada repo: ranking, trending, HN, Reddit, Bluesky, X, TikTok.
CREATE TABLE IF NOT EXISTS menciones (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id       INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    fuente        TEXT NOT NULL CHECK (fuente IN
                    ('github_ranking', 'github_trending', 'hackernews',
                     'reddit', 'bluesky', 'x', 'tiktok')),
    post_url      TEXT NOT NULL,
    autor         TEXT,
    texto         TEXT,
    publicado     TEXT,
    interaccion   INTEGER NOT NULL DEFAULT 0,      -- likes/puntos/upvotes según la fuente
    capturado     TEXT NOT NULL,
    UNIQUE (repo_id, post_url)
);

-- Menciones en redes cuyo repo todavía no se pudo confirmar en la API de GitHub
-- (p. ej. por el límite sin token). Se resuelven en corridas siguientes y pasan a `menciones`.
CREATE TABLE IF NOT EXISTS menciones_pendientes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name     TEXT NOT NULL,                   -- "owner/nombre" tal como apareció en el post
    fuente        TEXT NOT NULL CHECK (fuente IN ('hackernews', 'reddit', 'bluesky', 'x', 'tiktok')),
    post_url      TEXT NOT NULL,
    autor         TEXT,
    texto         TEXT,
    publicado     TEXT,
    interaccion   INTEGER NOT NULL DEFAULT 0,
    capturado     TEXT NOT NULL,
    intentos      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (full_name, post_url)
);

-- Lista cerrada de categorías por funcionalidad.
CREATE TABLE IF NOT EXISTS categorias (
    slug    TEXT PRIMARY KEY,
    nombre  TEXT NOT NULL
);

INSERT OR IGNORE INTO categorias (slug, nombre) VALUES
    ('ia-llm',         'IA y modelos de lenguaje'),
    ('agentes',        'Agentes y automatización con IA'),
    ('devtools',       'Herramientas para desarrolladores'),
    ('frontend',       'Frontend y diseño web'),
    ('backend',        'Backend y APIs'),
    ('datos',          'Datos y analítica'),
    ('seguridad',      'Seguridad'),
    ('infra',          'Infraestructura y DevOps'),
    ('movil',          'Apps móviles'),
    ('multimedia',     'Imagen, audio y video'),
    ('productividad',  'Productividad y herramientas personales'),
    ('educacion',      'Aprendizaje y recursos'),
    ('otros',          'Otros');

-- Resultado de la IA por repo. readme_sha permite no reprocesar si el README no cambió.
CREATE TABLE IF NOT EXISTS enriquecimiento (
    repo_id          INTEGER PRIMARY KEY REFERENCES repos(id) ON DELETE CASCADE,
    categoria        TEXT NOT NULL REFERENCES categorias(slug),
    descripcion_uso  TEXT NOT NULL,
    ejemplo_1        TEXT NOT NULL,
    ejemplo_2        TEXT NOT NULL,
    modelo           TEXT NOT NULL,
    readme_sha       TEXT,
    generado         TEXT NOT NULL
);

-- Traducciones al español de descripciones y textos de redes. La clave es el hash del texto original:
-- si la descripción cambia en GitHub, se traduce de nuevo; si no, nunca se repite el trabajo.
CREATE TABLE IF NOT EXISTS traducciones (
    hash      TEXT PRIMARY KEY,                    -- sha1 del texto original
    origen    TEXT NOT NULL,
    idioma    TEXT NOT NULL,                       -- idioma detectado: es, en, zh, ja, ko, ru, otro
    texto_es  TEXT,                                -- NULL si no hay modelo para ese idioma
    motor     TEXT NOT NULL,                       -- original (ya era español), argos-local, claude
    creado    TEXT NOT NULL
);

-- Resultado diario del detector de tendencia (fase 2).
CREATE TABLE IF NOT EXISTS tendencias (
    repo_id          INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    fecha            TEXT NOT NULL,                -- YYYY-MM-DD
    delta_dia        REAL,                         -- estrellas ganadas por día desde la foto anterior
    base_diaria      REAL,                         -- promedio diario de la semana previa
    score            REAL,                         -- delta_dia / max(base_diaria, piso)
    nivel            TEXT NOT NULL CHECK (nivel IN
                       ('explosivo', 'en_alza', 'nuevo_rapido', 'estable', 'sin_datos')),
    confianza        TEXT NOT NULL CHECK (confianza IN ('alta', 'media', 'baja')),
    dias_historial   INTEGER NOT NULL,
    en_trending      INTEGER NOT NULL DEFAULT 0,   -- apareció hoy en github.com/trending
    estrellas_hoy_trending INTEGER,                -- "N stars today" según la página de trending
    PRIMARY KEY (repo_id, fecha)
);

CREATE INDEX IF NOT EXISTS idx_tendencias_nivel ON tendencias(fecha, nivel);

-- Bitácora de corridas: la usa el healthcheck y la señal de vida de la fase 6.
CREATE TABLE IF NOT EXISTS corridas (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tarea    TEXT NOT NULL,
    inicio   TEXT NOT NULL,
    fin      TEXT,
    estado   TEXT NOT NULL CHECK (estado IN ('en_curso', 'ok', 'error')),
    detalle  TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshots_fecha ON snapshots(fecha);
CREATE INDEX IF NOT EXISTS idx_menciones_fuente ON menciones(fuente, capturado);
CREATE INDEX IF NOT EXISTS idx_corridas_tarea ON corridas(tarea, inicio);
