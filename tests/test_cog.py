"""Prueba del cog sin Discord real: stub de musica_core.nodos, canal falso y embeds."""
import asyncio, os, sys, types, tempfile, subprocess, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- stub de tus nodos ---
class Pista:
    def __init__(self, title, author, seg, uri="http://x"):
        self.title, self.author, self.length, self.is_stream, self.uri, self.artwork = title, author, int(seg * 1000), False, uri, "http://art"
llamadas = []
async def buscar(q):
    llamadas.append(q)
    if q.startswith("https://cdn.discordapp.com"):
        return [Pista("segmento", "maxia", 7.8, q)]
    if q.startswith("ytmsearch:"):
        return [Pista("Under Pressure (Live)", "x", 240), Pista("Under Pressure - Remastered", "Queen Official", 248)]
    return []
nodos = types.ModuleType("musica_core.nodos")
nodos.buscar_tracks_con_fallback = buscar
nodos.conectar_player = nodos.limpiar_voz = lambda *a, **k: None
class NodoFalso:
    def __init__(self, ident, http):
        self.identifier, self.http = ident, http
    async def fetch_info(self):
        return types.SimpleNamespace(source_managers=["youtube", "http"] if self.http else ["youtube"])
NODOS = [NodoFalso("node0", False), NodoFalso("node1", True)]
nodos.get_nodos_conectados = lambda: NODOS
pkg = types.ModuleType("musica_core"); pkg.nodos = nodos
sys.modules["musica_core"], sys.modules["musica_core.nodos"] = pkg, nodos

import wavelink
fetch_llamadas = []
async def fetch_falso(url, node=None):
    fetch_llamadas.append((url[:30], node.identifier))
    return [Pista("segmento", "maxia", 7.8, url)]
wavelink.Pool.fetch_tracks = staticmethod(fetch_falso) if not isinstance(wavelink.Pool.__dict__.get("fetch_tracks"), classmethod) else classmethod(lambda cls, url, node=None: fetch_falso(url, node))
from cogs.radio import Radio, barra_progreso
from radio_core import config as C

class Adjunto: url = "https://cdn.discordapp.com/attachments/1/2/seg.mp3?ex=1"
class Msg:
    attachments = [Adjunto()]
    async def delete(self): pass
class Canal:
    enviados = 0
    async def send(self, file=None, **kw):
        Canal.enviados += 1
        assert os.path.exists(file.fp.name), "no se adjuntó el archivo"
        return Msg()

async def main():
    r = Radio.__new__(Radio)
    from collections import deque
    r._subidos, r._aviso_canal, r._info_nodos = deque(), False, {}
    r._canal_archivos = lambda: Canal()
    ruta = tempfile.mktemp(suffix=".mp3")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1", ruta], check=True)

    t = await Radio._publicar(r, ruta)
    print("publicar →", t.title, t.length / 1000, "s | subidas:", Canal.enviados, "| lo leyó el nodo:", fetch_llamadas[-1][1])
    assert t.length == 7800 and Canal.enviados == 1 and len(r._subidos) == 1
    assert fetch_llamadas == [("https://cdn.discordapp.com/att", "node1")], "debía usar solo el nodo que acepta http"

    c = await Radio._buscar_cancion(r, "Queen", "Under Pressure")
    print("buscar →", c["track"].title)
    assert "Remastered" in c["track"].title and c["duracion"] == 248

    r._canal_archivos = lambda: None
    assert await Radio._publicar(r, ruta) is None          # sin canal configurado no revienta
    print("sin canal configurado → None (y avisa una sola vez)")

    # --- TTS: usa el Gemini del cog TTS, espera cupo y cae a edge ---
    import radio_core.config as CC
    CC.TTS_ESPERA_S = 0.05
    class SinCupo(RuntimeError): pass
    modtts = types.ModuleType("fake_tts_mod"); modtts.GEMINI_KEYS = ["k1"]; modtts.GeminiNoCapacity = SinCupo
    modtts._is_rate_limit = lambda e: "429" in str(e)
    class TTS:
        def __init__(self): self.gemini_calls, self.edge_calls, self.modo = 0, 0, "cupo_despues"
        async def _gemini_tts(self, texto, ruta):
            self.gemini_calls += 1
            if self.modo == "cupo_despues" and self.gemini_calls < 3: raise SinCupo("sin cupo")
            if self.modo == "roto": raise RuntimeError("modelo inválido")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1", str(ruta)], check=True)
        async def _edge_tts(self, texto, voz, ruta):
            self.edge_calls += 1
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "libmp3lame", str(ruta)], check=True)
            return True
    TTS.__module__ = "fake_tts_mod"; sys.modules["fake_tts_mod"] = modtts
    tts = TTS()
    r.bot = types.SimpleNamespace(get_cog=lambda n: tts if n == "TTS" else None)
    TEXTO1, TEXTO2 = f"Hola, soy Maxia {uuid.uuid4().hex} ✨", f"Otro texto {uuid.uuid4().hex} para el respaldo"
    modtts.GEMINI_TTS_MODEL, modtts.GEMINI_TTS_VOICE, modtts.GEMINI_STYLE = "gemini-3.8-flash-lite-tts", "Puck", "alegre"
    r._cfg_gemini = types.MethodType(Radio._cfg_gemini, r)
    assert r._cfg_gemini() == {"voz": "Puck", "estilo": "alegre", "modelo": "google/gemini-3.8-flash-lite-tts"}, r._cfg_gemini()
    ruta1 = await Radio._tts(r, TEXTO1)
    print("TTS con cupo tras esperar →", os.path.basename(ruta1), f"(gemini {tts.gemini_calls} intentos, edge {tts.edge_calls})")
    assert ruta1.endswith(".wav") and tts.gemini_calls == 3 and tts.edge_calls == 0
    tts.modo, tts.gemini_calls = "roto", 0
    ruta2 = await Radio._tts(r, TEXTO2)
    print("TTS con Gemini roto →", os.path.basename(ruta2), f"(gemini {tts.gemini_calls}, edge {tts.edge_calls})")
    assert ruta2.endswith(".mp3") and tts.gemini_calls == 1 and tts.edge_calls == 1, "un error que no es de cupo no debe reintentar"
    tts.gemini_calls = 0
    assert await Radio._tts(r, TEXTO1) == ruta1 and tts.gemini_calls == 0, "debía usar la caché"

    base = {"al_aire": True, "tipo": "idle", "programa": None, "voz_activa": False, "titulo": None, "artista": None, "portada": None,
            "url": None, "pedida_por": None, "pos": 0, "dur": 0, "noticia": None, "siguiente": None, "oyentes": 2}
    for nombre, est in {"idle": base, "voz": {**base, "tipo": "voz", "siguiente": "Vehicle — The Ides of March", "programa": "Leyendas del Rock"},
                        "cancion": {**base, "tipo": "cancion", "titulo": "Vehicle", "artista": "The Ides of March", "url": "https://youtu.be/x", "pos": 65, "dur": 180,
                                    "portada": "https://i.ytimg.com/a.jpg", "pedida_por": "Pepe"},
                        "noticia": {**base, "tipo": "noticia", "noticia": {"titulo": "Titular", "pais": "México", "fuente": "El Universal", "url": "https://x.mx"}}}.items():
        e = Radio._embed(r, est)
        print(f"[{nombre}] {e.description!r} campos={[f.name for f in e.fields]}")
    print("✅ COG OK")

asyncio.run(main())
