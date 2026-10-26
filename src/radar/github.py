"""Cliente mínimo de la API de GitHub: búsqueda de repos con manejo de límites y reintentos."""

import time
from collections.abc import Callable

import httpx

GITHUB_API = "https://api.github.com"
INTENTOS = 3
ESPERA_MAXIMA = 70  # segundos que se acepta esperar a que se renueve el límite


class GitHubError(Exception):
    """Falla que no se resolvió con reintentos."""


class LimiteAgotado(GitHubError):
    """Se acabó el límite de peticiones y la renovación tarda demasiado."""


class NoEncontrado(GitHubError):
    """GitHub respondió 404."""


class GitHubClient:
    """Sin token funciona en modo anónimo: 10 búsquedas/min y 60 peticiones/hora en lugar de 30 y 5000."""

    def __init__(
        self,
        token: str = "",
        client: httpx.Client | None = None,
        dormir: Callable[[float], None] = time.sleep,
        reloj: Callable[[], float] = time.time,
    ):
        self._client = client or httpx.Client(timeout=20, follow_redirects=True)
        self._dormir = dormir
        self._reloj = reloj
        self.anonimo = not token
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "radar-repos",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    def _espera_por_limite(self, resp: httpx.Response) -> float | None:
        """Segundos a esperar si la respuesta es un límite de peticiones; None si no lo es."""
        if resp.status_code not in (403, 429):
            return None
        if "retry-after" in resp.headers:  # límite secundario (abuso)
            return float(resp.headers["retry-after"])
        if resp.headers.get("x-ratelimit-remaining") == "0":
            reinicio = float(resp.headers.get("x-ratelimit-reset", self._reloj()))
            return max(reinicio - self._reloj(), 0) + 1
        return None

    def _get(self, url: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
        for intento in range(1, INTENTOS + 1):
            try:
                resp = self._client.get(url, params=params, headers=headers or self._headers)
            except httpx.TransportError as e:
                if intento == INTENTOS:
                    raise GitHubError(f"Sin conexión con GitHub: {e}") from e
                self._dormir(2 ** (intento - 1))
                continue

            espera = self._espera_por_limite(resp)
            if espera is not None:
                if espera > ESPERA_MAXIMA or intento == INTENTOS:
                    raise LimiteAgotado(f"Límite de GitHub agotado; se renueva en {espera:.0f} s")
                self._dormir(espera)
                continue
            if resp.status_code >= 500 and intento < INTENTOS:
                self._dormir(2 ** (intento - 1))
                continue
            if resp.status_code == 401:
                raise GitHubError("Token de GitHub inválido o vencido (401)")
            if resp.status_code == 404:
                raise NoEncontrado(f"No existe: {url}")
            if resp.status_code != 200:
                raise GitHubError(f"GitHub respondió {resp.status_code}: {resp.text[:200]}")
            return resp
        raise AssertionError("inalcanzable")

    def buscar_repos(self, consulta: str, paginas: int = 1, por_pagina: int = 100) -> list[dict]:
        """Repos que cumplen la consulta, ordenados por estrellas de mayor a menor.

        La Search API devuelve como máximo 1000 resultados (10 páginas de 100).
        """
        items: list[dict] = []
        for pagina in range(1, min(paginas, 10) + 1):
            resp = self._get(
                f"{GITHUB_API}/search/repositories",
                params={"q": consulta, "sort": "stars", "order": "desc", "per_page": por_pagina, "page": pagina},
            )
            lote = resp.json().get("items", [])
            items.extend(lote)
            if len(lote) < por_pagina:  # no hay más páginas
                break
        return items

    def obtener_repo(self, full_name: str) -> dict:
        """Datos del repo con el mismo formato que un item de la búsqueda."""
        return self._get(f"{GITHUB_API}/repos/{full_name}").json()

    def obtener_repo_por_id(self, repo_id: int) -> dict | None:
        """Por id sigue funcionando aunque el repo cambie de nombre. None si ya no existe."""
        try:
            return self._get(f"{GITHUB_API}/repositories/{repo_id}").json()
        except NoEncontrado:
            return None

    def repo_existe(self, full_name: str) -> bool:
        try:
            self.obtener_repo(full_name)
        except NoEncontrado:
            return False
        return True

    def pagina_html(self, url: str) -> str:
        """Página web pública de GitHub (p. ej. /trending). No envía el token."""
        return self._get(url, headers={"User-Agent": "radar-repos", "Accept": "text/html"}).text

    def close(self) -> None:
        self._client.close()
