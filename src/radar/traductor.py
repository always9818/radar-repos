"""Traductor al español para todo lo que muestra el panel (descripciones de repos y textos de redes).

Motor local: modelos de Argos Translate ejecutados con CTranslate2 (sin claves, sin enviar datos fuera).
Los idiomas sin modelo directo a español pasan por inglés (zh → en → es).

Pasos de cada corrida:
  1. Junta los textos que el panel muestra y que todavía no tienen traducción guardada.
  2. Detecta el idioma; si ya es español se guarda tal cual.
  3. Traduce protegiendo URLs, `código` y emojis; frase por frase para no perder calidad.
  4. Loop de verificación por texto: salida no vacía, sin tokens rotos, largo razonable y que no siga
     en inglés. Si falla, reintenta con más calidad (beam 4); si vuelve a fallar, queda sin traducción
     y el panel no lo muestra.

Uso:  uv run radar-traducir [--descargar] [--limite N]
"""

import argparse
import hashlib
import io
import json
import re
import sqlite3
import sys
import time
import tomllib
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import httpx

from radar import db
from radar.config import RAIZ, cargar_config

AJUSTES = RAIZ / "config" / "traductor.toml"
FUENTES_CON_TEXTO = ("hackernews", "reddit", "bluesky", "x", "tiktok")
MOTOR_LOCAL = "argos-local"

ES = {"el", "la", "los", "las", "de", "del", "que", "y", "para", "con", "una", "un", "es", "en", "por",
      "se", "su", "sus", "como", "más", "tu", "tus", "al", "lo", "sin", "sobre", "este", "esta", "herramienta"}
EN = {"the", "and", "for", "with", "to", "of", "a", "an", "is", "in", "your", "you", "that", "this", "on",
      "from", "by", "it", "are", "be", "as", "into", "using", "use", "built", "based", "open", "source", "tool"}

# Lo que no se traduce: URLs (también sin https://, como "github.com/a/b" en Bluesky), `código`,
# hashtags/menciones y emojis/símbolos pictográficos.
PROTEGIDO = re.compile(
    r"(https?://\S+|\b(?:[\w-]+\.)+(?:com|org|io|dev|net|app|ai|co|gg|sh|me|so|xyz)/\S*"
    r"|`[^`]+`|[#@][\w.-]+|[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]+)"
)
FIN_DE_FRASE = re.compile(r"(?<=[.!?。！？])\s+")


def cargar_ajustes(ruta: Path = AJUSTES) -> dict:
    return tomllib.loads(ruta.read_text(encoding="utf-8"))


def huella(texto: str) -> str:
    return hashlib.sha1(texto.encode("utf-8")).hexdigest()


# ---------- Detección de idioma ----------

def detectar_idioma(texto: str) -> str:
    letras = [c for c in texto if c.isalpha()]
    if not letras:
        return "es"  # solo emojis, números o símbolos: no hay nada que traducir
    total = len(letras)

    def proporcion(desde: int, hasta: int) -> float:
        return sum(1 for c in letras if desde <= ord(c) <= hasta) / total

    if proporcion(0xAC00, 0xD7AF) > 0.2:
        return "ko"
    if proporcion(0x3040, 0x30FF) > 0.1:
        return "ja"
    if proporcion(0x4E00, 0x9FFF) > 0.15:
        return "zh"
    if proporcion(0x0400, 0x04FF) > 0.3:
        return "ru"
    palabras = re.findall(r"[a-záéíóúñü]+", texto.lower())
    puntos_es = sum(p in ES for p in palabras) + 2 * len(re.findall(r"[ñ¿¡áéíóú]", texto.lower()))
    puntos_en = sum(p in EN for p in palabras)
    return "es" if puntos_es > puntos_en else "en"


def _puntos(texto: str) -> tuple[int, int]:
    """(señales de español, señales de inglés) en el texto."""
    palabras = re.findall(r"[a-záéíóúñü]+", texto.lower())
    es = sum(p in ES for p in palabras) + 2 * len(re.findall(r"[ñ¿¡áéíóú]", texto.lower()))
    return es, sum(p in EN for p in palabras)


def sigue_en_ingles(texto: str) -> bool:
    es, en = _puntos(texto)
    return en >= 2 and en > es


def _copia(origen: str, traducido: str) -> bool:
    """El modelo devolvió el original casi igual (le pasa con frases en Title Case)."""
    norma = lambda s: re.sub(r"[^\w]", "", s.lower())  # noqa: E731
    return len(re.findall(r"[A-Za-z]{3,}", origen)) >= 2 and norma(origen) == norma(traducido)


# ---------- Motor local (Argos + CTranslate2) ----------

class MotorLocal:
    nombre = MOTOR_LOCAL

    def __init__(self, ajustes: dict, directorio: Path | None = None):
        self.ajustes = ajustes
        self.dir = directorio or RAIZ / ajustes["directorio"]
        self._cache: dict[str, tuple] = {}

    def instalado(self, par: str) -> bool:
        return (self.dir / par / "model" / "model.bin").exists() and (self.dir / par / "sentencepiece.model").exists()

    def ruta(self, idioma: str) -> list[str] | None:
        """Pares de modelos para llevar `idioma` a español, o None si falta alguno."""
        pares = ["en_es"] if idioma == "en" else [f"{idioma}_en", "en_es"]
        return pares if all(self.instalado(p) for p in pares) else None

    def _modelo(self, par: str):
        if par not in self._cache:
            import ctranslate2
            import sentencepiece as spm

            self._cache[par] = (
                ctranslate2.Translator(str(self.dir / par / "model"), device="cpu"),
                spm.SentencePieceProcessor(model_file=str(self.dir / par / "sentencepiece.model")),
            )
        return self._cache[par]

    def traducir(self, frases: list[str], par: str, beam: int) -> list[str]:
        if not frases:
            return []
        traductor, sp = self._modelo(par)
        tokens = [sp.encode(f, out_type=str) for f in frases]
        salida = traductor.translate_batch(tokens, beam_size=beam, max_decoding_length=256)
        return [limpiar("".join(r.hypotheses[0])) for r in salida]


def nombres_propios(frase: str, glosario: Iterable[str] = ()) -> set[str]:
    """Palabras que deben sobrevivir a la traducción: términos del glosario y nombres con mayúscula
    interna, dígitos o punto (Scrapy, TypeScript, Node.js, GPT4…), salvo la primera palabra común."""
    nombres = {g.lower() for g in glosario if re.search(rf"(?<![\w.]){re.escape(g)}(?!\w)", frase, re.I)}
    palabras = re.findall(r"[A-Za-z][\w.+#-]*[\w+#]|[A-Za-z]", frase)
    largas = [p for p in palabras if len(p) > 3]
    # En Title Case ("JavaScript Style Guide") la mayúscula inicial no indica nombre propio.
    titulo = len(largas) >= 2 and sum(p[:1].isupper() for p in largas) / len(largas) > 0.6
    for i, p in enumerate(palabras):
        mayuscula = p[:1].isupper() and i > 0 and p.lower() not in EN and not titulo
        especial = re.search(r"[a-z][A-Z]|\d|\.", p) or mayuscula
        if especial and len(p) > 2:
            nombres.add(p.lower())
    return nombres


def mejor_en_es(
    motor, frases: list[str], beam: int, glosario: Iterable[str] = (), correcciones: dict[str, str] | None = None
) -> list[str]:
    """Traduce cada frase en 3 variantes y elige la que más suena a español sin perder nombres propios.

    El modelo tiende a copiar sin traducir frases en Title Case ("Free and Open Source Search Engine")
    porque las toma por nombres propios; con punto final o en minúsculas sí las traduce, pero en minúsculas
    a veces traduce nombres ("Rust" → "óxido"). Por eso se penaliza perder nombres del original.
    """
    n_var = 5
    variantes = []
    for f in frases:
        base = f.rstrip().rstrip(".")
        con_punto = f if f.rstrip().endswith((".", "!", "?", ":")) else base + "."
        variantes += [
            f,
            con_punto,
            con_punto.lower(),
            # Títulos cortos ("JavaScript Style Guide") solo se traducen enmarcados en una oración.
            f"Description: {base}.",
            f"It is a {base.lower()}.",
        ]
    glosario = list(glosario)
    salida = [aplicar_glosario(s, glosario, correcciones or {}) for s in motor.traducir(variantes, "en_es", beam)]
    elegidas = []
    for i, f in enumerate(frases):
        opciones = salida[n_var * i : n_var * i + n_var]
        opciones[3] = re.sub(r"^\s*(Descripción|Description)\s*:\s*", "", opciones[3])
        opciones[4] = re.sub(r"^\s*(Es|Esta es|Este es|Se trata de)\s+(un|una|el|la|a)?\s*", "", opciones[4], flags=re.I)
        nombres = nombres_propios(f, glosario)

        def puntuar(t: str) -> int:
            es, en = _puntos(t)
            perdidos = sum(1 for n in nombres if n not in t.lower())
            return es - 2 * en - (5 if _copia(f, t) else 0) - 4 * perdidos

        mejor = max(range(n_var), key=lambda k: (puntuar(opciones[k]), -k))
        texto = opciones[mejor].strip()
        if mejor > 0 and not f.rstrip().endswith(".") and texto.endswith("."):
            texto = texto.rstrip(".")  # el punto lo agregamos nosotros
        if mejor in (2, 4):
            texto = recuperar_mayusculas(texto, f)
        if f[:1].isupper():
            texto = texto[:1].upper() + texto[1:]
        elegidas.append(texto)
    return elegidas


def recuperar_mayusculas(traducido: str, original: str) -> str:
    """Las variantes en minúsculas pierden la escritura de nombres ("DeepSeek" → "deepseek"): se restaura."""
    formas = {p.lower(): p for p in re.findall(r"[\w.+#-]+", original) if re.search(r"[A-Z]", p)}
    return re.sub(r"[\w.+#-]+", lambda m: formas.get(m.group(0).lower(), m.group(0)), traducido)


def aplicar_glosario(texto: str, glosario: list[str], correcciones: dict[str, str]) -> str:
    """Restaura nombres técnicos que el modelo tradujo o puso en minúsculas."""
    for mal, bien in correcciones.items():
        texto = re.sub(rf"(?<!\w){re.escape(mal)}(?!\w)", bien, texto)
    for termino in sorted(glosario, key=len, reverse=True):
        texto = re.sub(rf"(?<![\w.]){re.escape(termino)}(?![\w])", termino, texto, flags=re.I)
    return texto


def limpiar(texto: str) -> str:
    """Quita marcas del tokenizador ('▁' = espacio, '⁇' = token desconocido)."""
    texto = texto.replace("▁", " ").replace("⁇", "")
    texto = re.sub(r"\s+([,.;:!?)])", r"\1", texto)
    return re.sub(r"\s{2,}", " ", texto).strip()


def enmascarar(texto: str, terminos: Iterable[str]) -> tuple[str, dict[str, str]]:
    """Cambia nombres propios por marcadores (N1, N2…) que el modelo deja intactos.

    Sin esto, "Scrapy" sale como "Rastrillado" y "Rust" como "Óxido".
    """
    prefijo = next(p for p in ("N", "X", "Q", "ZQ") if not re.search(rf"\b{p}\d", texto))
    mapa: dict[str, str] = {}
    for termino in sorted(set(terminos), key=len, reverse=True):
        def poner(m: re.Match) -> str:
            marca = f"{prefijo}{len(mapa) + 1}"
            mapa[marca] = m.group(0)
            return marca
        texto = re.sub(rf"(?<![\w.]){re.escape(termino)}(?![\w])", poner, texto, flags=re.I)
    return texto, mapa


def restaurar(texto: str, mapa: dict[str, str]) -> tuple[str, bool]:
    """Devuelve los nombres originales. ok=False si el modelo perdió algún marcador."""
    ok = True
    for marca, original in sorted(mapa.items(), key=lambda kv: -len(kv[0])):
        texto, n = re.subn(rf"\b{re.escape(marca)}\b", lambda _: original, texto, flags=re.I)
        ok = ok and n > 0
    return texto, ok


def _trozos(texto: str) -> list[tuple[bool, str]]:
    """(se_traduce, trozo) alternando texto normal y partes protegidas."""
    partes = []
    pos = 0
    for m in PROTEGIDO.finditer(texto):
        if m.start() > pos:
            partes.append((True, texto[pos:m.start()]))
        partes.append((False, m.group(0)))
        pos = m.end()
    if pos < len(texto):
        partes.append((True, texto[pos:]))
    return partes


def traducir_texto(
    motor, texto: str, idioma: str, beam: int, max_caracteres: int,
    glosario: list[str] = (), correcciones: dict[str, str] | None = None, nombres_extra: Iterable[str] = (),
) -> str | None:
    """Traduce un texto completo al español. None si no hay modelo o si la verificación falla dos veces.

    nombres_extra: nombres que no deben traducirse en este texto (p. ej. el nombre del repo: "Scrapy").
    """
    pares = motor.ruta(idioma)
    if pares is None:
        return None
    texto = texto.strip()[:max_caracteres]
    # Solo se protegen los nombres del repo que aparecen escritos como nombre propio: con mayúscula
    # ("Scrapy") o con símbolos ("scikit-learn"). "skills" en minúscula es una palabra común y se traduce.
    def como_nombre(n: str) -> list[str]:
        """Formas en que `n` aparece escrito como nombre propio (se respeta su escritura: "Vue", no "vue")."""
        return [
            m.group(0)
            for m in re.finditer(rf"(?<![\w.]){re.escape(n)}(?![\w])", texto, re.I)
            if re.search(r"[A-Z]|[^A-Za-z]", m.group(0))
        ]

    glosario = list(glosario) + [forma for n in nombres_extra if len(n) > 2 for forma in como_nombre(n)]
    for intento_beam in (beam, max(beam, 4)):
        piezas = []
        for traducible, trozo in _trozos(texto):
            if not traducible or not re.search(r"[^\W\d_]", trozo):
                piezas.append(trozo)
                continue
            borde_izq = trozo[: len(trozo) - len(trozo.lstrip())]
            borde_der = trozo[len(trozo.rstrip()):]

            def pasar(contenido: str) -> str:
                frases = [f for f in FIN_DE_FRASE.split(contenido) if f]
                for par in pares:
                    if par == "en_es":
                        frases = mejor_en_es(motor, frases, intento_beam, glosario, correcciones)
                    else:
                        frases = motor.traducir(frases, par, intento_beam)
                return " ".join(frases)

            enmascarado, mapa = enmascarar(trozo.strip(), glosario if idioma == "en" else [])
            traducido, ok = restaurar(pasar(enmascarado), mapa)
            if not ok:  # el modelo perdió un marcador: se traduce sin marcadores
                traducido = pasar(trozo.strip())
            # El glosario solo toca texto traducido, nunca URLs ni `código` (GitHub.com ≠ github.com).
            traducido = aplicar_glosario(traducido, list(glosario), correcciones or {})
            piezas.append(borde_izq + traducido + borde_der)
        resultado = re.sub(r"\s{2,}", " ", "".join(piezas)).strip()
        if valida(texto, resultado):
            return resultado
    # Último recurso: si el texto es solo nombres ("Visual Studio Code", "Nano vLLM", "CVE-2026-1234"),
    # el modelo lo copia porque no hay nada que traducir, y está bien mostrarlo tal cual.
    if idioma == "en" and solo_nombres(texto, glosario):
        return texto
    return None


def solo_nombres(texto: str, glosario: Iterable[str]) -> bool:
    """El texto es solo nombres propios o códigos: fuera del glosario quedan como mucho 2 palabras
    distintas, ninguna es una palabra común en inglés y el texto es corto."""
    resto, _ = enmascarar(texto, glosario)
    libres = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", resto) if not re.fullmatch(r"(N|X|Q|ZQ)\d+", w)}
    return len(texto.split()) <= 4 and len(libres) <= 2 and not libres & EN


def valida(origen: str, traducido: str) -> bool:
    if not traducido or "⁇" in traducido or "▁" in traducido:
        return False
    proporcion = len(traducido) / max(len(origen), 1)
    if len(origen) > 20 and not 0.25 <= proporcion <= 4:
        return False
    return not sigue_en_ingles(traducido) and not _copia(origen, traducido)


# ---------- Descarga de modelos ----------

def descargar_modelos(ajustes: dict, http: httpx.Client, motor: MotorLocal) -> list[str]:
    """Descarga y extrae los modelos que falten. Devuelve los problemas encontrados."""
    problemas = []
    for par, url in ajustes["modelos"].items():
        if motor.instalado(par):
            continue
        try:
            resp = http.get(url, follow_redirects=True, timeout=300)
            resp.raise_for_status()
            instalar_paquete(resp.content, par, motor.dir)
        except (httpx.HTTPError, ValueError, zipfile.BadZipFile) as e:
            problemas.append(f"Modelo {par}: {e}")
    return problemas


def instalar_paquete(contenido: bytes, par: str, directorio: Path) -> None:
    """Extrae solo model/, sentencepiece.model y metadata.json, verificando idiomas y rutas."""
    origen, destino_idioma = par.split("_")
    with zipfile.ZipFile(io.BytesIO(contenido)) as z:
        meta_nombre = next((n for n in z.namelist() if n.count("/") == 1 and n.endswith("metadata.json")), None)
        if not meta_nombre:
            raise ValueError("el paquete no trae metadata.json")
        meta = json.loads(z.read(meta_nombre))
        if (meta.get("from_code"), meta.get("to_code")) != (origen, destino_idioma):
            raise ValueError(f"el paquete es {meta.get('from_code')}→{meta.get('to_code')}, no {origen}→{destino_idioma}")
        base = (directorio / par).resolve()
        for info in z.infolist():
            partes = PurePosixPath(info.filename).parts
            if info.is_dir() or len(partes) < 2 or partes[1] not in ("model", "sentencepiece.model", "metadata.json"):
                continue
            destino = (base / Path(*partes[1:])).resolve()
            if base not in destino.parents:
                raise ValueError(f"ruta sospechosa dentro del paquete: {info.filename}")
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(z.read(info))
    if not (base / "model" / "model.bin").exists() or not (base / "sentencepiece.model").exists():
        raise ValueError("al paquete le faltan model.bin o sentencepiece.model")


# ---------- Textos de la base ----------

def textos_a_traducir(con: sqlite3.Connection, reintentar: bool = False) -> list[tuple[str, list[str]]]:
    """(texto, nombres a proteger) de lo que muestra el panel y todavía no está en `traducciones`.

    reintentar=True incluye también los que antes fallaron o no tenían modelo (texto_es NULL).
    """
    filas = con.execute(
        f"""
        SELECT descripcion_gh AS texto, full_name FROM repos WHERE archivado = 0 AND descripcion_gh IS NOT NULL
        UNION ALL
        SELECT m.texto, r.full_name FROM menciones m JOIN repos r ON r.id = m.repo_id
        WHERE m.texto IS NOT NULL AND m.fuente IN ({','.join('?' * len(FUENTES_CON_TEXTO))})
        """,
        FUENTES_CON_TEXTO,
    ).fetchall()
    ya = {f[0] for f in con.execute(
        "SELECT hash FROM traducciones" + (" WHERE texto_es IS NOT NULL" if reintentar else "")
    )}
    unicos: dict[str, tuple[str, list[str]]] = {}
    for texto, full_name in filas:
        if texto and texto.strip() and (h := huella(texto)) not in ya:
            # "owner/mi-repo" → ["owner", "mi-repo", "mi", "repo"]: cualquiera puede aparecer como nombre propio.
            partes = full_name.replace("/", " ").split()
            nombres = partes + [p for parte in partes for p in re.split(r"[-_.]", parte)]
            unicos.setdefault(h, (texto, nombres))
    return list(unicos.values())


def buscar(con: sqlite3.Connection, textos: Iterable[str]) -> dict[str, sqlite3.Row]:
    """{texto original: fila de traducciones} para los textos que ya se procesaron."""
    por_hash = {huella(t): t for t in textos if t}
    filas: dict[str, sqlite3.Row] = {}
    hashes = list(por_hash)
    for i in range(0, len(hashes), 500):
        lote = hashes[i : i + 500]
        for f in con.execute(f"SELECT * FROM traducciones WHERE hash IN ({','.join('?' * len(lote))})", lote):
            filas[por_hash[f["hash"]]] = f
    return filas


def en_espanol(con: sqlite3.Connection, textos: Iterable[str]) -> dict[str, str | None]:
    """{texto original: versión en español o None si no se pudo traducir}. Textos sin procesar no aparecen."""
    return {t: f["texto_es"] for t, f in buscar(con, textos).items()}


@dataclass
class Informe:
    pendientes: int = 0
    traducidos: int = 0
    ya_en_espanol: int = 0
    sin_modelo: dict[str, int] = field(default_factory=dict)
    fallidos: int = 0
    segundos: float = 0.0
    problemas: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problemas


def traducir_pendientes(
    con: sqlite3.Connection,
    motor,
    ajustes: dict,
    limite: int | None = None,
    progreso: Callable[[int, int], None] | None = None,
    reintentar: bool = False,
) -> Informe:
    inf = Informe()
    inicio = time.time()
    textos = textos_a_traducir(con, reintentar)
    if limite:
        textos = textos[:limite]
    inf.pendientes = len(textos)
    for i, (texto, nombres) in enumerate(textos, 1):
        idioma = detectar_idioma(texto)
        if idioma == "es":
            texto_es, nombre_motor = texto, "original"
            inf.ya_en_espanol += 1
        elif motor.ruta(idioma) is None:
            texto_es, nombre_motor = None, "sin-modelo"
            inf.sin_modelo[idioma] = inf.sin_modelo.get(idioma, 0) + 1
        else:
            texto_es = traducir_texto(
                motor, texto, idioma, ajustes["beam"], ajustes["max_caracteres"],
                ajustes.get("glosario", []), ajustes.get("correcciones", {}), nombres,
            )
            nombre_motor = motor.nombre
            if texto_es is None:
                inf.fallidos += 1
            else:
                inf.traducidos += 1
        con.execute(
            "INSERT OR REPLACE INTO traducciones (hash, origen, idioma, texto_es, motor, creado) VALUES (?, ?, ?, ?, ?, ?)",
            (huella(texto), texto, idioma, texto_es, nombre_motor, db.ahora()),
        )
        if i % 50 == 0:
            con.commit()
            if progreso:
                progreso(i, len(textos))
    con.commit()
    inf.segundos = round(time.time() - inicio, 1)

    # Verificación final: no debe quedar ningún texto del panel sin procesar (salvo si se usó --limite).
    if not limite and (faltan := len(textos_a_traducir(con))):
        inf.problemas.append(f"Quedaron {faltan} textos sin procesar")
    # La tasa de fallos se mide sobre todo lo traducido, no solo sobre esta corrida (con --reintentar
    # la corrida son justo los difíciles).
    total, fallidos = con.execute(
        "SELECT COUNT(*), SUM(texto_es IS NULL AND motor = ?) FROM traducciones", (motor.nombre,)
    ).fetchone()
    if total and (fallidos or 0) / total > 0.1:
        inf.problemas.append(f"{fallidos} de {total} traducciones no pasaron la verificación (más del 10 %)")
    return inf


def main() -> int:
    parser = argparse.ArgumentParser(description="Traduce al español los textos del panel")
    parser.add_argument("--descargar", action="store_true", help="descarga los modelos que falten (~80-90 MB c/u)")
    parser.add_argument("--limite", type=int, help="traducir como máximo N textos (para probar)")
    parser.add_argument("--reintentar", action="store_true", help="volver a intentar los que fallaron o no tenían modelo")
    parser.add_argument("--solo-descargar", action="store_true", help="solo descargar los modelos que falten, sin traducir")
    args = parser.parse_args()
    args.descargar = args.descargar or args.solo_descargar
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ajustes = cargar_ajustes()
    motor = MotorLocal(ajustes)
    if args.descargar:
        with httpx.Client(headers={"User-Agent": "radar-repos"}) as http:
            for p in descargar_modelos(ajustes, http, motor):
                print(f"❌ {p}")
    faltan = [par for par in ajustes["modelos"] if not motor.instalado(par)]
    if "en_es" in faltan:
        print("❌ Falta el modelo inglés→español. Corre: uv run radar-traducir --descargar")
        return 1
    if args.solo_descargar:
        print(f"✅ Modelos instalados: {', '.join(p for p in ajustes['modelos'] if motor.instalado(p))}")
        return 0 if not faltan else 1

    cfg = cargar_config()
    con = db.conectar(cfg.database_path)
    try:
        db.aplicar_esquema(con)
        corrida = db.iniciar_corrida(con, "traductor")
        inf = traducir_pendientes(con, motor, ajustes, args.limite, lambda i, n: print(f"   {i}/{n}…"), args.reintentar)
        db.cerrar_corrida(con, corrida, inf.ok, json.dumps(inf.__dict__, ensure_ascii=False))
    finally:
        con.close()

    print(f"Textos nuevos: {inf.pendientes} · traducidos: {inf.traducidos} · ya en español: {inf.ya_en_espanol} · "
          f"fallidos: {inf.fallidos} · {inf.segundos} s")
    for idioma, n in inf.sin_modelo.items():
        print(f"⚠️  {n} textos en '{idioma}' sin modelo instalado: no se mostrarán (agrega {idioma}_en en traductor.toml)")
    for p in inf.problemas:
        print(f"❌ {p}")
    print("\nTraducciones al día." if inf.ok else "\nHubo fallas en el traductor.")
    return 0 if inf.ok else 1


if __name__ == "__main__":
    sys.exit(main())
