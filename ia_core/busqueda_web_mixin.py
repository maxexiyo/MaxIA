"""
BusquedaWebMixin v4
-------------------
1) search_duckduckgo(query) -> str        (MISMA firma de siempre: no rompe ia_core)
   caché -> MOTOR PROPIO (SearXNG local + Wikipedia + lectura de páginas) -> Gemini + Google Search
   -> Google CSE (opcional) -> Brave (opcional, con tope mensual) -> Google News RSS
   -> scrapers en cadena (último recurso: son lentos y los bloquean)
2) obtener_noticias_latam(...) -> list[dict]   RSS (sin keys, sin scraping)  -> para el noticiero
3) obtener_exitos(pais) -> list[dict]          Apple Music "most-played" por país (sin key) -> para programas

Variables de entorno (todas opcionales):
  SEARXNG_URL            ej. http://127.0.0.1:8888  (tu SearXNG propio)
  GOOGLE_CSE_API_KEY, GOOGLE_CSE_ID
  BRAVE_SEARCH_API_KEY, BRAVE_MAX_MES (default 900)
"""
import asyncio
import html as _html
import json
import os
import random
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus, urlparse

import aiohttp

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TTL_WEB = 600            # 10 min: la misma búsqueda no se repite
TTL_NOTICIAS = 900       # 15 min
TTL_EXITOS = 6 * 3600    # 6 h (Apple actualiza la lista una vez al día)
GAP_SCRAPERS = 1.5       # seg. mínimos entre dos requests a los scrapers (evita el 202 por ráfagas)
BRAVE_USO_ARCHIVO = "brave_uso.json"
PROPIO_PAGINAS = 2       # cuántas páginas usa el motor propio por búsqueda
PROPIO_CHARS = 280       # cuánto texto saca de cada página
PROPIO_MAX_TOTAL = 650   # tope de caracteres de TODO lo que recibe la IA
PROPIO_LEER = 6          # cuántos resultados descarga y compara (se queda con los mejores)
PROPIO_MIN_RELEVANCIA = 0.5   # fracción de palabras de la consulta que debe contener la página; si ninguna llega, devuelve None y pasa al siguiente motor
STOPWORDS = set("""que quien cual cuales como donde cuando cuanto los las del una uno unos unas por para con sin sobre
entre este esta esto ese esa eso son fue ser hay mas muy ahora hoy mismo dime busca buscar internet web ponla pon
llamada dame the and what who how is are""".split())

# País -> nombre. Códigos de Google News (gl / ceid).
PAISES_LATAM = {
    "MX": "México", "AR": "Argentina", "CO": "Colombia", "CL": "Chile", "PE": "Perú",
    "VE": "Venezuela", "EC": "Ecuador", "UY": "Uruguay", "BO": "Bolivia", "PY": "Paraguay",
    "CR": "Costa Rica", "PA": "Panamá", "DO": "República Dominicana", "GT": "Guatemala",
    "HN": "Honduras", "SV": "El Salvador", "NI": "Nicaragua", "CU": "Cuba", "PR": "Puerto Rico",
}
# Temas de Google News: WORLD, NATION, BUSINESS, TECHNOLOGY, ENTERTAINMENT, SPORTS, SCIENCE, HEALTH
# Feeds con resumen real (Google News solo trae titular + fuente). Revisa que sigan vivos.
FEEDS_EXTRA = [
    ("BBC Mundo", "https://feeds.bbci.co.uk/mundo/rss.xml"),
    ("France 24", "https://www.france24.com/es/rss"),
    ("DW Español", "https://rss.dw.com/xml/rss-sp-all"),
]
# Transmisión compartida por varios servidores: se evitan titulares muy gráficos.
FILTRO_NOTICIAS = re.compile(
    r"asesin|masacre|feminicid|violaci[oó]n|decapit|descuartiz|balacera|ejecutad|cad[aá]ver|"
    r"suicid|abuso sexual|pederast|tortur",
    re.IGNORECASE,
)


class BusquedaWebMixin:
    _CACHE: dict = {}
    _scrape_lock = None
    _ultimo_scrape = 0.0

    # ───────────────────────── caché ─────────────────────────
    @classmethod
    def _cache_get(cls, clave):
        item = cls._CACHE.get(clave)
        if item and item[0] > time.time():
            return item[1]
        cls._CACHE.pop(clave, None)
        return None

    @classmethod
    def _cache_set(cls, clave, valor, ttl):
        if len(cls._CACHE) > 400:
            cls._CACHE.clear()
        cls._CACHE[clave] = (time.time() + ttl, valor)

    # ───────────────────── limpieza / parsers de scrapers ─────────────────────
    @staticmethod
    def _limpiar_texto(txt: str) -> str:
        txt = re.sub(r"<[^>]+>", " ", txt or "")
        txt = _html.unescape(txt)
        return re.sub(r"\s+", " ", txt).strip()

    @staticmethod
    def _limpiar_snippets(snippets_crudos: list) -> list:
        limpios = []
        for snip in snippets_crudos[:5]:
            clean = BusquedaWebMixin._limpiar_texto(snip)
            if clean:
                limpios.append(clean)
        return limpios

    def _parsear_ddg_html(self, html: str) -> list:
        return self._limpiar_snippets(re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', html, re.DOTALL))

    def _parsear_ddg_lite(self, html: str) -> list:
        return self._limpiar_snippets(re.findall(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', html, re.DOTALL))

    def _parsear_mojeek(self, html: str) -> list:
        return self._limpiar_snippets(re.findall(r'<p class="s"[^>]*>(.*?)</p>', html, re.DOTALL))

    def _parsear_bing(self, html: str) -> list:
        snippets = re.findall(r"<p>(.*?)</p>", html, re.DOTALL)
        return self._limpiar_snippets([s for s in snippets if len(self._limpiar_texto(s)) > 40])

    def _parsear_startpage(self, html: str) -> list:
        return self._limpiar_snippets(
            re.findall(r'<p[^>]*class="[^"]*description[^"]*"[^>]*>(.*?)</p>', html, re.DOTALL))

    def _parsear_ecosia(self, html: str) -> list:
        return self._limpiar_snippets(
            re.findall(r'<p[^>]*class="[^"]*result__description[^"]*"[^>]*>(.*?)</p>', html, re.DOTALL))

    async def _intentar_busqueda(self, session, metodo, url, query, parser, extra_data=None):
        headers = {"User-Agent": UA, "Accept-Language": "es-MX,es;q=0.9,en;q=0.8"}
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            if metodo == "POST":
                data = {"q": query}
                data.update(extra_data or {})
                cm = session.post(url, data=data, headers=headers, timeout=timeout)
            else:
                cm = session.get(url, headers=headers, timeout=timeout)
            async with cm as resp:
                if resp.status == 200:
                    snippets = parser(await resp.text())
                    if snippets:
                        return " | ".join(snippets), None
                    return None, "sin resultados (posible cambio de HTML o 0 resultados reales)"
                if resp.status == 202:
                    return None, "202 (rate-limit suave, IP marcada temporalmente)"
                return None, f"status {resp.status}"
        except asyncio.TimeoutError:
            return None, "timeout"
        except Exception as e:
            return None, f"excepción: {e}"

    # ───────────────────────── MOTOR PROPIO ─────────────────────────
    async def _candidatos_searxng(self, session, query: str) -> list:
        """Resultados de TU SearXNG (SEARXNG_URL). [{url, titulo, snippet}]"""
        base = (os.getenv("SEARXNG_URL") or "").rstrip("/")
        if not base:
            return []
        try:
            async with session.get(f"{base}/search",
                                   params={"q": query, "format": "json", "language": "es"},
                                   headers={"User-Agent": UA},
                                   timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status != 200:
                    print(f"⚠️ SearXNG status {r.status} (¿falta 'json' en formats del settings.yml?)")
                    return []
                data = await r.json(content_type=None)
        except Exception as e:
            print(f"⚠️ SearXNG no responde: {e}")
            return []
        return [{"url": x.get("url"), "titulo": self._limpiar_texto(x.get("title")),
                 "snippet": self._limpiar_texto(x.get("content"))}
                for x in (data.get("results") or [])[:8] if x.get("url")]

    async def _candidatos_wikipedia(self, session, query: str) -> list:
        url = ("https://es.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=3"
               f"&srsearch={quote_plus(query)}")
        try:
            async with session.get(url, headers={"User-Agent": UA},
                                   timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return []
                data = await r.json(content_type=None)
        except Exception:
            return []
        salida = []
        for x in (data.get("query") or {}).get("search", []):
            titulo = x.get("title", "")
            salida.append({"url": "https://es.wikipedia.org/wiki/" + quote_plus(titulo.replace(" ", "_")),
                           "titulo": titulo, "snippet": self._limpiar_texto(x.get("snippet"))})
        return salida

    async def _leer_pagina(self, session, url: str, max_chars: int = PROPIO_CHARS):
        """Descarga una página y saca solo los párrafos de texto útiles."""
        try:
            async with session.get(url, headers={"User-Agent": UA, "Accept-Language": "es-MX,es;q=0.9"},
                                   timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200 or "html" not in r.headers.get("Content-Type", "").lower():
                    return None
                crudo = await r.content.read(500_000)       # tope: no bajar páginas gigantes
                html = crudo.decode(r.get_encoding() or "utf-8", errors="ignore")
        except Exception:
            return None
        html = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form|noscript|svg)[^>]*>.*?</\1>", " ", html)
        parrafos = [self._limpiar_texto(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", html)]
        texto = " ".join(p for p in parrafos if len(p) > 60)
        return self._recortar(texto, max_chars) or None

    @staticmethod
    def _recortar(texto: str, n: int) -> str:
        """Corta a n caracteres, preferiblemente al final de una oración."""
        if len(texto) <= n:
            return texto
        corte = texto[:n]
        punto = max(corte.rfind(". "), corte.rfind("? "), corte.rfind("! "))
        return corte[:punto + 1] if punto > n * 0.5 else corte.rstrip() + "…"

    @staticmethod
    def _norm(t: str) -> str:
        t = unicodedata.normalize("NFD", (t or "").lower())
        return "".join(c for c in t if unicodedata.category(c) != "Mn")

    def _terminos(self, query: str) -> set:
        return {w for w in re.findall(r"\w+", self._norm(query)) if len(w) > 2 and w not in STOPWORDS}

    def _puntaje(self, texto: str, terminos: set) -> float:
        """Fracción de palabras clave de la consulta que aparecen en el texto (0 a 1)."""
        if not terminos:
            return 1.0
        t = self._norm(texto)
        return sum(1 for w in terminos if w in t) / len(terminos)

    def _mejor_fragmento(self, texto: str, terminos: set, n: int) -> str:
        """En vez de tomar el inicio de la página, arranca en la oración que más coincide con la consulta."""
        frases = re.split(r"(?<=[.!?])\s+", texto)
        if len(frases) < 2 or not terminos:
            return self._recortar(texto, n)
        mejor = max(range(len(frases)),
                    key=lambda i: (sum(1 for w in terminos if w in self._norm(frases[i])), -i))
        return self._recortar(" ".join(frases[mejor:]), n)

    async def _buscar_propio(self, query: str):
        """Motor propio: SearXNG (tu instancia) + Wikipedia -> abre las mejores páginas y extrae el texto."""
        async with aiohttp.ClientSession() as s:
            sx, wk = await asyncio.gather(self._candidatos_searxng(s, query),
                                          self._candidatos_wikipedia(s, query))
            candidatos, vistos = [], set()
            for c in sx[:4] + wk[:2] + sx[4:]:               # mezcla: web + Wikipedia
                if c["url"] not in vistos:
                    vistos.add(c["url"])
                    candidatos.append(c)
            candidatos = candidatos[:PROPIO_LEER]
            if not candidatos:
                return None
            textos = await asyncio.gather(*[self._leer_pagina(s, c["url"], 2500) for c in candidatos])
        terminos = self._terminos(query)
        puntuados = []
        for c, texto in zip(candidatos, textos):
            cuerpo = texto or c["snippet"]                   # sin página legible: usa el snippet
            if cuerpo:
                p = self._puntaje(f"{c['titulo']} {c['snippet']} {cuerpo}", terminos)
                puntuados.append((p, c, cuerpo))
        puntuados.sort(key=lambda x: -x[0])                  # primero los más relevantes, no los primeros que salieron
        partes, dominios = [], set()
        for p, c, cuerpo in puntuados:
            if p < PROPIO_MIN_RELEVANCIA:
                break
            dom = urlparse(c["url"]).netloc.replace("www.", "")
            if dom in dominios:
                continue
            dominios.add(dom)
            partes.append(f"{self._mejor_fragmento(cuerpo, terminos, PROPIO_CHARS)} ({dom})")
            if len(partes) >= PROPIO_PAGINAS:
                break
        if not partes:
            print(f"⚠️ Motor propio: nada relevante para '{query}', paso al siguiente motor")
            return None                                       # search_duckduckgo sigue con Gemini/CSE/Brave/GNews/scrapers
        return self._recortar(" | ".join(partes), PROPIO_MAX_TOTAL)

    # ───────────────────────── APIs opcionales ─────────────────────────
    async def _buscar_google_cse(self, query: str):
        key, cx = os.getenv("GOOGLE_CSE_API_KEY"), os.getenv("GOOGLE_CSE_ID")
        if not key or not cx:
            return None
        url = (f"https://www.googleapis.com/customsearch/v1?key={key}&cx={cx}"
               f"&q={quote_plus(query)}&num=5&lr=lang_es")
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    print(f"⚠️ Google CSE status {r.status}")
                    return None
                data = await r.json()
        items = data.get("items") or []
        partes = [f"{self._limpiar_texto(i.get('title'))}: {self._limpiar_texto(i.get('snippet'))}"
                  for i in items[:5]]
        return " | ".join(p for p in partes if len(p) > 3) or None

    def _brave_puede_usarse(self) -> bool:
        """Tope mensual propio: Brave cobra por uso y puede no tener tope de gasto en su cuenta."""
        tope = int(os.getenv("BRAVE_MAX_MES", "900"))
        mes = time.strftime("%Y-%m")
        try:
            with open(BRAVE_USO_ARCHIVO) as f:
                uso = json.load(f)
        except Exception:
            uso = {}
        if uso.get("mes") != mes:
            uso = {"mes": mes, "n": 0}
        if uso["n"] >= tope:
            return False
        uso["n"] += 1
        try:
            with open(BRAVE_USO_ARCHIVO, "w") as f:
                json.dump(uso, f)
        except Exception:
            pass
        return True

    async def _buscar_brave(self, query: str):
        key = os.getenv("BRAVE_SEARCH_API_KEY")
        if not key or not self._brave_puede_usarse():
            return None
        url = (f"https://api.search.brave.com/res/v1/web/search?q={quote_plus(query)}"
               f"&count=5&search_lang=es&country=mx")
        headers = {"Accept": "application/json", "X-Subscription-Token": key}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    print(f"⚠️ Brave status {r.status}")
                    return None
                data = await r.json()
        res = (data.get("web") or {}).get("results") or []
        partes = [f"{self._limpiar_texto(i.get('title'))}: {self._limpiar_texto(i.get('description'))}"
                  for i in res[:5]]
        return " | ".join(p for p in partes if len(p) > 3) or None

    # ───────────────── Gemini con Google Search (usa tus mismas keys) ─────────────────
    _gemini_search_idx = 0

    @staticmethod
    def _parsear_respuesta_gemini(data: dict):
        """Saca el texto y las fuentes de una respuesta de Gemini con grounding. Pura (se prueba sin red)."""
        cand = ((data or {}).get("candidates") or [{}])[0]
        partes = (cand.get("content") or {}).get("parts") or []
        texto = re.sub(r"\s+", " ", " ".join(p.get("text", "") for p in partes)).strip()
        if not texto:
            return None
        chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks") or []
        fuentes = []
        for c in chunks:
            titulo = ((c or {}).get("web") or {}).get("title", "")
            if titulo and titulo not in fuentes:
                fuentes.append(titulo)
        return texto + (f" (Fuentes: {', '.join(fuentes[:3])})" if fuentes else "")

    async def _buscar_gemini(self, query: str):
        """Búsqueda real de Google a través de Gemini. Va en una llamada APARTE (Gemini no deja mezclar
        google_search con function calling en el mismo request). Respeta la cuota gratis de tu cuenta:
        si da 429 rota de key, si da 404/503 prueba el siguiente modelo."""
        try:
            from ia_core.config import GEMINI_API_KEYS
            from ia_core.tools_schema import GEMINI_MODELS_FALLBACK
        except Exception:
            return None
        if not GEMINI_API_KEYS:
            return None
        body = {
            "contents": [{"parts": [{"text": (
                "Busca en internet y responde en español, en máximo 120 palabras, con datos concretos y actuales. "
                "Si no encuentras nada confiable, dilo claramente. Consulta: " + query)}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 400},
        }
        intentos = 0
        async with aiohttp.ClientSession() as s:
            for modelo in GEMINI_MODELS_FALLBACK[:2]:
                for _ in range(min(len(GEMINI_API_KEYS), 3)):
                    intentos += 1
                    idx = BusquedaWebMixin._gemini_search_idx % len(GEMINI_API_KEYS)
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
                    try:
                        async with s.post(url, json=body, headers={"x-goog-api-key": GEMINI_API_KEYS[idx]},
                                          timeout=aiohttp.ClientTimeout(total=25)) as r:
                            if r.status == 200:
                                res = self._parsear_respuesta_gemini(await r.json())
                                if res:
                                    return res
                                break   # respondió pero sin texto: no insistir con más keys
                            print(f"⚠️ Gemini búsqueda ({modelo}) status {r.status}")
                            if r.status == 429:
                                BusquedaWebMixin._gemini_search_idx += 1   # key agotada: la siguiente
                                continue
                            break       # 404/503/otro: probar con el siguiente modelo
                    except Exception as e:
                        print(f"⚠️ Gemini búsqueda ({modelo}) falló: {e}")
                        break
        return None

    # ───────────────── Google News RSS como búsqueda de noticias recientes ─────────────────
    async def _buscar_gnews(self, query: str):
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=es-419&gl=MX&ceid=MX:es-419"
        async with aiohttp.ClientSession() as s:
            items = await self._bajar_feed(s, url, "", "MX")
        titulares = [f"{i['titulo']} ({i['fuente']})" if i["fuente"] else i["titulo"] for i in items[:5]]
        if not titulares:
            return None
        return "Titulares de noticias recientes relacionados: " + " | ".join(titulares)

    # ───────────────────────── scrapers en cadena ─────────────────────────
    def _motores_scrapers(self, query: str) -> list:
        q = quote_plus(query)
        return [
            ("DDG html", "POST", "https://html.duckduckgo.com/html/", self._parsear_ddg_html, 2, {"kl": "mx-es"}),
            ("DDG lite", "POST", "https://lite.duckduckgo.com/lite/", self._parsear_ddg_lite, 1, {"kl": "mx-es"}),
            ("Mojeek", "GET", f"https://www.mojeek.com/search?q={q}", self._parsear_mojeek, 1, None),
            ("Bing", "GET", f"https://www.bing.com/search?q={q}", self._parsear_bing, 1, None),
            ("Startpage", "GET", f"https://www.startpage.com/sp/search?query={q}", self._parsear_startpage, 1, None),
            ("Ecosia", "GET", f"https://www.ecosia.org/search?q={q}", self._parsear_ecosia, 1, None),
        ]

    async def _buscar_scrapers(self, query: str):
        motores = self._motores_scrapers(query)
        if BusquedaWebMixin._scrape_lock is None:
            BusquedaWebMixin._scrape_lock = asyncio.Lock()
        # Un scrape a la vez y con pausa mínima: las ráfagas son lo que dispara el 202 de DDG.
        async with BusquedaWebMixin._scrape_lock:
            async with aiohttp.ClientSession() as session:
                for nombre, metodo, url, parser, intentos, extra in motores:
                    for intento in range(1, intentos + 1):
                        espera = GAP_SCRAPERS - (time.monotonic() - BusquedaWebMixin._ultimo_scrape)
                        if espera > 0:
                            await asyncio.sleep(espera)
                        resultado, motivo = await self._intentar_busqueda(session, metodo, url, query, parser, extra)
                        BusquedaWebMixin._ultimo_scrape = time.monotonic()
                        if resultado:
                            return resultado
                        print(f"⚠️ Búsqueda web falló en {nombre} (intento {intento}/{intentos}): {motivo}")
                        if intento < intentos:
                            await asyncio.sleep(2 * intento)
        print("⚠️ Búsqueda web falló en TODOS los motores disponibles")
        return None

    # ───────────────────────── API pública: búsqueda ─────────────────────────
    async def search_duckduckgo(self, query: str) -> str:
        """Mismo nombre y retorno de siempre (texto o 'Error en búsqueda web')."""
        query = (query or "").strip()
        if not query:
            return "Error en búsqueda web"
        clave = ("web", query.lower())
        hit = self._cache_get(clave)
        if hit:
            return hit
        for nombre, fn in (("Propio", self._buscar_propio), ("Gemini", self._buscar_gemini),
                           ("Google CSE", self._buscar_google_cse), ("Brave", self._buscar_brave),
                           ("Google News", self._buscar_gnews)):
            try:
                res = await fn(query)
            except Exception as e:
                print(f"⚠️ {nombre} falló: {e}")
                res = None
            if res:
                self._cache_set(clave, res, TTL_WEB)
                return res
        res = await self._buscar_scrapers(query)
        if res:
            self._cache_set(clave, res, TTL_WEB)
            return res
        return "Error en búsqueda web"

    # ───────────────────────── noticias (RSS) ─────────────────────────
    @staticmethod
    def _parsear_rss(xml_texto: str, fuente_default: str = "", pais: str = "") -> list:
        """RSS 2.0 -> [{titulo, fuente, link, fecha, resumen, imagen, pais}]. Pura (se puede probar sin red)."""
        try:
            root = ET.fromstring(xml_texto)
        except ET.ParseError:
            return []
        limpiar = BusquedaWebMixin._limpiar_texto
        salida = []
        for it in root.iter("item"):
            titulo = limpiar(it.findtext("title"))
            link = (it.findtext("link") or "").strip()
            if not titulo or not link:
                continue
            fuente = limpiar(it.findtext("source")) or fuente_default
            # Google News pone "Titular - Fuente" en el título
            if fuente and titulo.endswith(f" - {fuente}"):
                titulo = titulo[: -len(fuente) - 3].strip()
            resumen = limpiar(it.findtext("description"))
            if len(resumen) < 40 or resumen.lower().startswith(titulo.lower()[:30]):
                resumen = ""   # Google News solo trae links repetidos, no un resumen real
            imagen = ""
            for el in it:
                etiqueta = el.tag.rsplit("}", 1)[-1]
                url = el.attrib.get("url", "")
                if url and (etiqueta in ("thumbnail", "content")
                            or (etiqueta == "enclosure" and el.attrib.get("type", "").startswith("image"))):
                    imagen = url
                    break
            salida.append({
                "titulo": titulo, "fuente": fuente, "link": link,
                "fecha": (it.findtext("pubDate") or "").strip(),
                "resumen": resumen[:300], "imagen": imagen, "pais": pais,
            })
        return salida

    async def _bajar_feed(self, session, url: str, fuente: str, pais: str = "") -> list:
        clave = ("rss", url)
        hit = self._cache_get(clave)
        if hit is not None:
            return hit
        try:
            async with session.get(url, headers={"User-Agent": UA},
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    print(f"⚠️ Feed {fuente or url} status {r.status}")
                    return []
                items = self._parsear_rss(await r.text(), fuente, pais)
        except Exception as e:
            print(f"⚠️ Feed {fuente or url} falló: {e}")
            return []
        self._cache_set(clave, items, TTL_NOTICIAS)
        return items

    async def obtener_noticias_latam(self, cantidad: int = 4, paises=None, tema: str = None,
                                     incluir_extra: bool = True) -> list:
        """Titulares para el noticiero. Mezcla países (round-robin) y quita repetidos y titulares gráficos.
        paises: lista de códigos ('MX','AR',...) o None = 5 al azar.  tema: 'WORLD','TECHNOLOGY','SPORTS',..."""
        paises = [p.upper() for p in (paises or random.sample(list(PAISES_LATAM), 5)) if p.upper() in PAISES_LATAM]
        feeds = []
        for p in paises:
            base = "https://news.google.com/rss"
            if tema:
                base += f"/headlines/section/topic/{tema.upper()}"
            feeds.append((f"{base}?hl=es-419&gl={p}&ceid={p}:es-419", "", p))
        if incluir_extra:
            feeds += [(u, nombre, "") for nombre, u in FEEDS_EXTRA]
        async with aiohttp.ClientSession() as s:
            listas = await asyncio.gather(*[self._bajar_feed(s, u, f, p) for u, f, p in feeds])
        vistos, resultado = set(), []
        for i in range(max((len(l) for l in listas), default=0)):      # round-robin entre fuentes
            for lista in listas:
                if i >= len(lista):
                    continue
                n = lista[i]
                firma = re.sub(r"\W+", "", n["titulo"].lower())[:50]
                if firma in vistos or FILTRO_NOTICIAS.search(n["titulo"] + " " + n["resumen"]):
                    continue
                vistos.add(firma)
                resultado.append(n)
        return resultado[:cantidad]

    # ───────────────────────── éxitos musicales ─────────────────────────
    @staticmethod
    def _parsear_exitos(data: dict) -> list:
        salida = []
        for pos, r in enumerate(((data or {}).get("feed") or {}).get("results") or [], 1):
            generos = [g.get("name", "") for g in (r.get("genres") or []) if g.get("name") not in ("Música", "Music")]
            salida.append({"posicion": pos, "titulo": r.get("name", ""), "artista": r.get("artistName", ""),
                           "genero": generos[0] if generos else "", "portada": r.get("artworkUrl100", ""),
                           "url": r.get("url", "")})
        return [x for x in salida if x["titulo"] and x["artista"]]

    async def obtener_exitos(self, pais: str = "mx", cantidad: int = 25) -> list:
        """Lo más escuchado en Apple Music del país (feed oficial, sin key). Útil para programas de éxitos."""
        pais = (pais or "mx").lower()
        n = next((x for x in (10, 25, 50, 100) if x >= cantidad), 100)
        clave = ("exitos", pais, n)
        hit = self._cache_get(clave)
        if hit is not None:
            return hit[:cantidad]
        for host in ("rss.marketingtools.apple.com", "rss.applemarketingtools.com"):
            url = f"https://{host}/api/v2/{pais}/music/most-played/{n}/songs.json"
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(url, headers={"User-Agent": UA},
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status != 200:
                            print(f"⚠️ Éxitos {host} status {r.status}")
                            continue
                        lista = self._parsear_exitos(await r.json(content_type=None))
            except Exception as e:
                print(f"⚠️ Éxitos {host} falló: {e}")
                continue
            if lista:
                self._cache_set(clave, lista, TTL_EXITOS)
                return lista[:cantidad]
        return []