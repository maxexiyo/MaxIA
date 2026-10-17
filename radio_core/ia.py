"""Cliente de IA de la radio (Groq → Gemini → OpenRouter, rotando keys). Toma las keys de tu config.py
(GROQ_API_KEYS / GROQ_API_KEY, GEMINI_API_KEYS y OPENROUTER_KEYS / OPENROUTER_KEY) o, si no están, de las
variables de entorno (varias keys separadas por coma). El orden se cambia con RADIO_IA_ORDEN.
Si ya tienes tu propio módulo de IA, pásale tu función al Director (ia=...) y este archivo ni se usa."""
import base64
import json
import logging
import os
import re

import aiohttp

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("RADIO_GROQ_MODEL", "openai/gpt-oss-120b")
GEMINI_MODEL = os.getenv("RADIO_GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# "openrouter/free" = OpenRouter elige al azar un modelo gratis. Para que Maxia siempre suene igual, fija uno:
# RADIO_OPENROUTER_MODEL=proveedor/modelo  (lo ves en https://openrouter.ai/models)
OPENROUTER_MODEL = os.getenv("RADIO_OPENROUTER_MODEL", "openrouter/free")
# Orden en que se prueban los proveedores de texto; los que no tengan keys se saltan solos.
ORDEN_IA = [x.strip() for x in os.getenv("RADIO_IA_ORDEN", "groq,gemini,openrouter").lower().split(",") if x.strip()]


def _separar(v) -> list:
    if isinstance(v, str):
        v = re.split(r"[,;\s]+", v)
    return [str(k).strip() for k in (v or []) if k and str(k).strip()]


def _claves(plural: str, singular: str, env: str) -> list:
    try:
        import config as cfg                      # el config.py de tu bot
        ks = _separar(getattr(cfg, plural, None) or getattr(cfg, singular, None))
        if ks:
            return ks
    except Exception:
        pass
    return _separar(os.getenv(env, ""))


def claves_groq() -> list:
    return _claves("GROQ_API_KEYS", "GROQ_API_KEY", "GROQ_API_KEY")


def claves_gemini() -> list:
    return _claves("GEMINI_API_KEYS", "GEMINI_API_KEY", "GEMINI_API_KEY") or _claves("GEMINI_API_KEYS", "GOOGLE_API_KEY", "GOOGLE_API_KEY")


def claves_openrouter() -> list:
    """Varias keys en OPENROUTER_KEY, separadas por coma (o una por línea). También sirve OPENROUTER_KEYS en config.py."""
    return _claves("OPENROUTER_KEYS", "OPENROUTER_KEY", "OPENROUTER_KEY")


_idx = {"groq": 0, "gemini": 0, "openrouter": 0, "openrouter_tts": 0}


async def _con_groq(prompt, sistema, json_mode, max_tokens, t, errores):
    llaves = claves_groq()
    for i in range(len(llaves)):
        k = (_idx["groq"] + i) % len(llaves)
        try:
            body = {"model": GROQ_MODEL, "temperature": 1.0, "max_tokens": max_tokens + 600, "reasoning_effort": "low",
                    "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": prompt}]}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            async with aiohttp.ClientSession(timeout=t) as s:
                async with s.post(GROQ_URL, json=body, headers={"Authorization": f"Bearer {llaves[k]}"}) as r:
                    data = await r.json(content_type=None)
                    if r.status == 200:
                        txt = (data["choices"][0]["message"].get("content") or "").strip()
                        if txt:
                            _idx["groq"] = k
                            return txt
                    errores.append(f"groq#{k + 1} {r.status}: {str(data)[:120]}")
        except Exception as e:
            errores.append(f"groq#{k + 1}: {e!r}")
    return None


async def _con_gemini(prompt, sistema, json_mode, max_tokens, t, errores):
    llaves = claves_gemini()
    for i in range(len(llaves)):
        k = (_idx["gemini"] + i) % len(llaves)
        try:
            cfg = {"temperature": 1.0, "maxOutputTokens": max_tokens}
            if json_mode:
                cfg["responseMimeType"] = "application/json"
            body = {"systemInstruction": {"parts": [{"text": sistema}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": cfg}
            async with aiohttp.ClientSession(timeout=t) as s:
                async with s.post(GEMINI_URL.format(modelo=GEMINI_MODEL), json=body,
                                  headers={"x-goog-api-key": llaves[k]}) as r:
                    data = await r.json(content_type=None)
                    if r.status == 200:
                        txt = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()
                        if txt:
                            _idx["gemini"] = k
                            return txt
                    errores.append(f"gemini#{k + 1} {r.status}: {str(data)[:120]}")
        except Exception as e:
            errores.append(f"gemini#{k + 1}: {e!r}")
    return None


async def _con_openrouter(prompt, sistema, json_mode, max_tokens, t, errores):
    """OpenRouter (API compatible con OpenAI). Rota entre las keys de OPENROUTER_KEY si una falla o se queda sin cupo."""
    llaves = claves_openrouter()
    if json_mode:
        # no todos los modelos aceptan response_format; se pide en el prompt y extraer_json() se encarga del resto
        sistema = (sistema + "\n\n" if sistema else "") + "Responde ÚNICAMENTE con un objeto JSON válido, sin texto antes ni después y sin ```."
    for i in range(len(llaves)):
        k = (_idx["openrouter"] + i) % len(llaves)
        try:
            body = {"model": OPENROUTER_MODEL, "temperature": 1.0, "max_tokens": max_tokens + 600,
                    "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": prompt}]}
            headers = {"Authorization": f"Bearer {llaves[k]}", "X-Title": "Radio Maxia"}
            async with aiohttp.ClientSession(timeout=t) as s:
                async with s.post(OPENROUTER_URL, json=body, headers=headers) as r:
                    data = await r.json(content_type=None)
                    if r.status == 200:
                        txt = ((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
                        if txt:
                            _idx["openrouter"] = k
                            return txt
                    errores.append(f"openrouter#{k + 1} {r.status}: {str(data)[:120]}")
        except Exception as e:
            errores.append(f"openrouter#{k + 1}: {e!r}")
    return None


_PROVEEDORES = {"groq": _con_groq, "gemini": _con_gemini, "openrouter": _con_openrouter}


async def pedir_ia(prompt: str, sistema: str = "", *, json_mode: bool = False, max_tokens: int = 900) -> str:
    """Devuelve el texto de la IA (proveedores en el orden de RADIO_IA_ORDEN). Lanza RuntimeError si todo falla."""
    errores = []
    t = aiohttp.ClientTimeout(total=45)
    for nombre in ORDEN_IA:
        fn = _PROVEEDORES.get(nombre)
        if fn is None:
            continue
        txt = await fn(prompt, sistema, json_mode, max_tokens, t, errores)
        if txt:
            return txt
    raise RuntimeError(("La IA no respondió: " + " | ".join(errores)) if errores
                       else "No hay keys de Groq, Gemini ni OpenRouter (config.py o variables de entorno)")


def extraer_json(texto: str):
    """Saca un dict de la respuesta de la IA aunque venga con ```json o texto alrededor."""
    if not texto:
        return None
    texto = re.sub(r"```(?:json)?", "", texto)
    i, j = texto.find("{"), texto.rfind("}")
    if i == -1 or j <= i:
        return None
    try:
        return json.loads(texto[i:j + 1])
    except Exception:
        return None


async def describir_imagen(url: str) -> str:
    """Descripción corta de una imagen (para que Maxia comente las fotos de los oyentes). '' si falla."""
    llaves = claves_gemini()
    if not llaves:
        return ""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return ""
                datos = await r.read()
                mime = (r.headers.get("Content-Type") or "image/png").split(";")[0]
            if len(datos) > 6_000_000 or not mime.startswith("image/"):
                return ""
            body = {"contents": [{"parts": [
                {"text": "Describe en una sola frase corta, en español, qué se ve en esta imagen. Sin opiniones."},
                {"inlineData": {"mimeType": mime, "data": base64.b64encode(datos).decode()}}]}],
                "generationConfig": {"maxOutputTokens": 120}}
            for k in llaves:
                async with s.post(GEMINI_URL.format(modelo=GEMINI_MODEL), json=body, headers={"x-goog-api-key": k}) as r:
                    data = await r.json(content_type=None)
                    if r.status == 200:
                        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()
    except Exception as e:
        logger.warning(f"[radio] no se pudo describir la imagen: {e!r}")
    return ""
