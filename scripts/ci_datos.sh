#!/usr/bin/env bash
# Guarda y recupera la base del radar en la rama `datos` del repositorio (para GitHub Actions).
#
#   bash scripts/ci_datos.sh bajar   # rama datos → data/radar.db (+ copia en data/radar_prev.db para comparar)
#   bash scripts/ci_datos.sh subir   # data/radar.db → rama datos (verifica antes y después)
#
# La rama `datos` tiene siempre un único commit con radar.db.gz: cada corrida lo reemplaza (push forzado),
# así el repositorio no crece día a día. El historial de respaldos queda en los artefactos de Actions (30 días).
# Usa el remoto `origin` de la copia actual, así hereda las credenciales que configura actions/checkout.

set -euo pipefail

RAMA="${RADAR_RAMA_DATOS:-datos}"
REMOTO="${RADAR_REMOTO:-origin}"
DB="data/radar.db"
PREV="data/radar_prev.db"
SUBIR="data/radar_subir.db"

bajar() {
  mkdir -p data
  if git fetch --quiet --depth 1 "$REMOTO" "refs/heads/$RAMA:refs/remotes/$REMOTO/$RAMA" 2>/dev/null; then
    git show "$REMOTO/$RAMA:radar.db.gz" | gzip -dc > "$DB"
    cp "$DB" "$PREV"
    echo "📥 Base recuperada de la rama $RAMA ($(wc -c < "$DB") bytes)"
  else
    rm -f "$PREV"
    echo "📭 No existe la rama $RAMA todavía: se empieza con una base vacía"
  fi
}

subir() {
  # 1. Copia consistente + chequeo de salud y comparación con la base anterior (no pisa una buena con una dañada).
  uv run radar-salud --solo-integridad --comparar "$PREV" --respaldo "$SUBIR"
  [ -s "$SUBIR" ] || { echo "❌ No se generó el respaldo"; exit 1; }

  # 2. Commit único sin padres con radar.db.gz, construido con comandos de bajo nivel (no toca la rama actual).
  local tmp blob arbol commit
  tmp="$(mktemp)"
  gzip -9 -c "$SUBIR" > "$tmp"
  blob="$(git hash-object -w "$tmp")"
  arbol="$(printf '100644 blob %s\tradar.db.gz\n' "$blob" | git mktree)"
  commit="$(
    GIT_AUTHOR_NAME="radar-bot" GIT_AUTHOR_EMAIL="radar-bot@users.noreply.github.com" \
    GIT_COMMITTER_NAME="radar-bot" GIT_COMMITTER_EMAIL="radar-bot@users.noreply.github.com" \
    git commit-tree "$arbol" -m "Base del radar · $(date -u +%Y-%m-%dT%H:%MZ)"
  )"
  git push --quiet --force "$REMOTO" "$commit:refs/heads/$RAMA"

  # 3. Verificación: lo que quedó en el remoto es exactamente lo que se subió.
  git fetch --quiet --depth 1 "$REMOTO" "refs/heads/$RAMA:refs/remotes/$REMOTO/$RAMA"
  if [ "$(git rev-parse "$REMOTO/$RAMA:radar.db.gz")" != "$blob" ]; then
    echo "❌ La rama $RAMA no quedó con la base subida"
    exit 1
  fi
  rm -f "$tmp"
  echo "📤 Base guardada en la rama $RAMA ($(wc -c < "$SUBIR") bytes, $(git cat-file -s "$blob") comprimida)"
}

case "${1:-}" in
  bajar) bajar ;;
  subir) subir ;;
  *) echo "Uso: $0 bajar|subir"; exit 2 ;;
esac
