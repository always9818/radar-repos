# Activa la corrida diaria en la nube (GitHub Actions + panel en GitHub Pages). Se corre UNA vez.
#
#   gh auth login                                                   # antes, una sola vez
#   powershell -ExecutionPolicy Bypass -File scripts\subir_a_github.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\subir_a_github.ps1 -Visibilidad private
#
# Pasos (si uno falla, se detiene sin seguir):
#   1. Verifica gh, git, bash y uv, y que hayas iniciado sesion en GitHub.
#   2. Corre todas las pruebas: si alguna falla, no sube nada.
#   3. Crea el repositorio local y verifica que .env, la base y los modelos NO se suban.
#   4. Crea el repositorio en GitHub y sube el codigo.
#   5. Sube la base actual a la rama `datos` (asi la nube sigue el historial que ya tienes).
#   6. Activa GitHub Pages para el panel y lanza la primera corrida en la nube.
#   7. Quita la tarea diaria de esta PC: dos copias escribiendo bases distintas se desincronizarian.
#
# Nota: GitHub Pages es gratis en repositorios publicos. En privados requiere un plan de pago.

param(
    [string]$Nombre = "radar-repos",
    [ValidateSet("public", "private")]
    [string]$Visibilidad = "public",
    [switch]$MantenerTareaLocal
)

$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

function Paso($texto) { Write-Output "`n==> $texto" }
function Fallar($texto) { Write-Output "ERROR: $texto"; exit 1 }

Paso "1/7 Verificando herramientas"
foreach ($cmd in "gh", "git", "uv") {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { Fallar "No se encontro '$cmd'." }
}
# El bash de Git para Windows, no el de WSL (C:\Windows\System32\bash.exe), que sin distribucion instalada falla.
$gitDir = Split-Path -Parent (Split-Path -Parent (Get-Command git).Source)
$bash = Join-Path $gitDir "bin\bash.exe"
if (-not (Test-Path $bash)) { Fallar "No se encontro el bash de Git en $bash" }
gh auth status 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Fallar "No has iniciado sesion en GitHub. Corre: gh auth login" }
$usuario = gh api user --jq .login
Write-Output "Sesion de GitHub: $usuario"

Paso "2/7 Corriendo las pruebas"
uv run pytest -q
if ($LASTEXITCODE -ne 0) { Fallar "Hay pruebas fallando: no se sube nada." }

Paso "3/7 Preparando el repositorio local"
if (-not (Test-Path ".git")) { git init -q -b main }
foreach ($privado in ".env", "data/radar.db", "modelos/en_es/model/model.bin", "panel/data.js") {
    if (Test-Path $privado) {
        git check-ignore -q $privado
        if ($LASTEXITCODE -ne 0) { Fallar "$privado no esta en .gitignore: no se sube nada." }
    }
}
git add -A
$staged = git diff --cached --name-only
if ($staged | Where-Object { $_ -match '(^|/)\.env$|\.db$|model\.bin$' }) { Fallar "Se iba a subir un archivo privado: $staged" }
if ($staged) {
    git -c user.name="$usuario" -c user.email="$usuario@users.noreply.github.com" commit -q -m @'
Radar de repos: recolectores, tendencia, redes, traductor, panel y corrida diaria

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
'@
}

Paso "4/7 Creando el repositorio en GitHub ($usuario/$Nombre, $Visibilidad)"
$remoto = git remote 2>$null | Where-Object { $_ -eq "origin" }
if (-not $remoto) {
    gh repo create $Nombre --$Visibilidad --source . --remote origin --push --description "Radar de repos de GitHub en tendencia, con panel en espanol"
    if ($LASTEXITCODE -ne 0) { Fallar "No se pudo crear el repositorio." }
} else {
    git push -u origin main
}

Paso "5/7 Subiendo la base actual a la rama datos"
& $bash scripts/ci_datos.sh subir
if ($LASTEXITCODE -ne 0) { Fallar "No se pudo subir la base." }

Paso "6/7 Activando GitHub Pages y lanzando la primera corrida"
gh api -X POST "repos/$usuario/$Nombre/pages" -f build_type=workflow 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Output "Aviso: no se pudo activar Pages automaticamente (en privados requiere plan de pago)."
    Write-Output "       Activalo en Settings > Pages > Source: GitHub Actions."
}
gh workflow run radar-diario.yml -f forzar=true
if ($LASTEXITCODE -ne 0) { Fallar "No se pudo lanzar la corrida en la nube." }
Write-Output "Primera corrida lanzada. Siguela con: gh run watch"

Paso "7/7 Tarea diaria de esta PC"
if ($MantenerTareaLocal) {
    Write-Output "Se mantiene la tarea local (-MantenerTareaLocal). Ojo: la PC y la nube guardaran bases distintas."
} else {
    & "$PSScriptRoot\programar_tarea.ps1" -Quitar
}

Write-Output "`nListo. Panel: https://$usuario.github.io/$Nombre/  (puede tardar unos minutos la primera vez)"
