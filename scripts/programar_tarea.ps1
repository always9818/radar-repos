# Registra (o actualiza) la corrida diaria del radar en el Programador de tareas de Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\programar_tarea.ps1               # todos los días a las 08:00
#   powershell -ExecutionPolicy Bypass -File scripts\programar_tarea.ps1 -Hora 19:30   # cambiar la hora
#   powershell -ExecutionPolicy Bypass -File scripts\programar_tarea.ps1 -Quitar       # eliminar la tarea
#
# - Corre con pythonw.exe: no abre ninguna ventana.
# - Si a esa hora la PC estaba apagada, Windows la corre en cuanto vuelva a estar disponible.
# - Corre solo con tu sesión iniciada (no pide contraseña) y también con batería.
# - El propio radar evita repetir el día si ya hubo una corrida completa.

param(
    [string]$Hora = "08:00",
    [switch]$Quitar
)

$ErrorActionPreference = "Stop"
$nombre = "Radar de repos - corrida diaria"

if ($Quitar) {
    if (Get-ScheduledTask -TaskName $nombre -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $nombre -Confirm:$false
        Write-Output "Tarea eliminada: $nombre"
    } else {
        Write-Output "No habia tarea programada: $nombre"
    }
    exit 0
}

$raiz = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $raiz ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    throw "No existe $pythonw. Corre primero 'uv sync' dentro de $raiz."
}

$accion = New-ScheduledTaskAction -Execute $pythonw -Argument "-m radar.diario" -WorkingDirectory $raiz
$disparador = New-ScheduledTaskTrigger -Daily -At $Hora
$ajustes = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $nombre -Action $accion -Trigger $disparador -Settings $ajustes `
    -Description "Radar de repos: ranking, tendencia, Hacker News, Reddit, Bluesky, traduccion y panel. Log en $raiz\logs" `
    -Force | Out-Null

$tarea = Get-ScheduledTask -TaskName $nombre
$info = $tarea | Get-ScheduledTaskInfo
Write-Output "Tarea registrada: $nombre"
Write-Output "Proxima corrida: $($info.NextRunTime)"
