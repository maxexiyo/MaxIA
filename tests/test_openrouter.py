"""Prueba de OpenRouter (texto + voz) contra un servidor falso local: rotación de keys y caída a la siguiente."""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["RADIO_DIR"] = tempfile.mkdtemp()
os.environ["OPENROUTER_KEY"] = "malaKey,\n buenaKey ; otraKey"      # varias keys, separadores mezclados
os.environ["RADIO_IA_ORDEN"] = "openrouter"
from aiohttp import web
from radio_core import ia, voz, config as C

vistas = []
async def chat(req):
    k = req.headers["Authorization"].split()[-1]; vistas.append(("chat", k)); b = await req.json()
    if k == "malaKey":
        return web.json_response({"error": "sin saldo"}, status=402)
    assert b["model"] == ia.OPENROUTER_MODEL and "JSON válido" in b["messages"][0]["content"]
    return web.json_response({"choices": [{"message": {"content": '{"ok": true}'}}]})
async def speech(req):
    k = req.headers["Authorization"].split()[-1]; vistas.append(("tts", k)); b = await req.json()
    if k == "malaKey":
        return web.json_response({"error": "rate limit"}, status=429)
    assert b["model"] == voz.MODELO_OR_DEFECTO and b["voice"] in ("Kore", "Puck") and b["response_format"] == "mp3" and b["instructions"]
    return web.Response(body=b"ID3" + b"\x00" * 5000, content_type="audio/mpeg")

async def main():
    app = web.Application(); app.router.add_post("/chat", chat); app.router.add_post("/speech", speech)
    runner = web.AppRunner(app); await runner.setup(); site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    puerto = site._server.sockets[0].getsockname()[1]
    ia.OPENROUTER_URL = f"http://127.0.0.1:{puerto}/chat"; voz.OR_TTS_URL = f"http://127.0.0.1:{puerto}/speech"

    assert ia.claves_openrouter() == ["malaKey", "buenaKey", "otraKey"], ia.claves_openrouter()
    txt = await ia.pedir_ia("hola", "sistema", json_mode=True)
    assert ia.extraer_json(txt) == {"ok": True}
    assert vistas == [("chat", "malaKey"), ("chat", "buenaKey")], vistas           # la mala falla y rota a la buena
    vistas.clear(); await ia.pedir_ia("otra", "s", json_mode=True)
    assert vistas == [("chat", "buenaKey")], "debe recordar la key que sí funciona"
    print("✔ texto: separa varias keys, rota ante error y recuerda la buena")

    vistas.clear(); C.TTS_MOTOR = "auto"
    ruta = await voz.sintetizar("Hola, soy Maxia y esta es una prueba.")
    assert ruta.endswith(".mp3") and os.path.getsize(ruta) > 1000 and "voz_o_" in ruta, ruta
    assert vistas == [("tts", "malaKey"), ("tts", "buenaKey")], vistas
    vistas.clear(); assert await voz.sintetizar("Hola, soy Maxia y esta es una prueba.") == ruta and not vistas, "debía usar la caché"
    r2 = await voz.sintetizar_openrouter("Otra frase con la voz del cog.", "Puck"); assert r2 and os.path.getsize(r2) > 1000   # voz del cog TTS
    print("✔ voz: OpenRouter genera el mp3, rota keys y usa caché")

    vistas.clear(); C.TTS_MOTOR = "edge"
    await voz.sintetizar("Texto distinto para edge, sin llamar a OpenRouter.")      # edge sin red falla → None, pero no toca OpenRouter
    assert not any(v[0] == "tts" for v in vistas)
    print("✔ motor=edge no usa OpenRouter")
    await runner.cleanup()

asyncio.run(main()); print("✅ OPENROUTER OK")
