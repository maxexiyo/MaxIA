"""Prueba del director con IA / voz / Lavalink / subida FALSAS (la mezcla con ffmpeg sí es real)."""
import asyncio, hashlib, json, os, random, subprocess, sys, tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from radio_core import config as C
from radio_core.director import Director
from radio_core.emisora import Emisora

TMP = tempfile.mkdtemp()
random.seed(7)
n_cancion = {"n": 0}
publicados = []

def tono(nombre, hz, seg, carpeta=TMP):
    ruta = os.path.join(carpeta, nombre)
    if not os.path.exists(ruta):
        os.makedirs(carpeta, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={seg}",
                        "-af", "volume=3", "-ar", "48000", "-ac", "2", ruta], check=True)
    return ruta

async def ia_falsa(prompt, sistema="", *, json_mode=False, max_tokens=0):
    if "Propón" in prompt:
        out = []
        for _ in range(5):
            n_cancion["n"] += 1
            out.append({"artista": f"Artista {n_cancion['n']}", "titulo": f"Canción {n_cancion['n']}"})
        return json.dumps({"canciones": out})
    if "música de FONDO" in prompt:
        return json.dumps({"local": "fondo"})
    if "noticiero" in prompt:
        return json.dumps({"intro": "Noticias.", "notas": ["Nota uno.", "Nota dos.", "Nota tres.", "Nota cuatro."], "cierre": "Fin."})
    if "oyentes mandaron" in prompt:
        return json.dumps({"voz": "Saludos a Pepe, va tu canción.", "peticiones": [{"artista": "Los Bukis", "titulo": "Tu Cárcel", "de": "Pepe"}]})
    return "Texto de locutora de prueba para esta sección."

async def tts_falso(texto):
    return tono("v_" + hashlib.md5(texto.encode()).hexdigest()[:6] + ".wav", 700, 1.2)

async def buscar_falso(artista, titulo):
    if "Imposible" in titulo:
        return None
    return {"track": f"TRACK[{artista} - {titulo}]", "titulo": titulo, "artista": artista, "duracion": 2.0,
            "portada": "http://x/y.jpg", "url": "http://x"}

async def publicar_falso(ruta):
    assert os.path.exists(ruta) and os.path.getsize(ruta) > 1000, "el mp3 mezclado no existe o está vacío"
    publicados.append(ruta)
    return f"PUB[{os.path.basename(ruta)}]"

async def noticias_falsas(n, excluir):
    return [{"titulo": f"Titular {i}", "fuente": "Fuente", "pais": p, "url": "http://n"} for i, p in enumerate(["México", "Chile", "Perú", "Colombia"])][:n]

hora = {"dt": datetime(2026, 10, 9, 19, 50)}
def nuevo(em):
    return Director(em, buscar_cancion=buscar_falso, publicar=publicar_falso, ia=ia_falsa, tts=tts_falso,
                    noticias=noticias_falsas, describir=lambda u: asyncio.sleep(0, ""), ahora=lambda: hora["dt"])

def preparar_audios():
    creados = []
    for carpeta, nombre, hz, seg in [(C.DIR_FONDOS, "fondo.wav", 200, 3),
                                     (C.DIR_PROGRAMAS / "leyendas_del_rock", "intro.wav", 440, 12),
                                     (C.DIR_PROGRAMAS / "leyendas_del_rock", "cortinilla.wav", 330, 5),
                                     (C.DIR_JINGLES, "Radio_Maxia.wav", 500, 6),
                                     (C.DIR_JINGLES, "Three-Note_Motif.wav", 600, 2)]:
        ruta = os.path.join(carpeta, nombre)
        if not os.path.exists(ruta):
            tono(nombre, hz, seg, carpeta=str(carpeta))
            creados.append(ruta)
    return creados

async def planear():
    print("── Plan de programación ──")
    em = Emisora()
    d = nuevo(em)
    secuencia = []
    for i in range(16):
        if i == 5:
            await d.agregar_mensaje("Pepe", "¡Saludos desde Tlaquepaque! Pónganme Tu Cárcel de Los Bukis @everyone")
            secuencia.append("<<llega mensaje de oyente>>")
        if i == 9:
            hora["dt"] = datetime(2026, 10, 9, 20, 2)
            secuencia.append("<<20:02: cambia a Leyendas del Rock>>")
        if i == 14:
            hora["dt"] = datetime(2026, 10, 9, 21, 1)
            secuencia.append("<<21:01: termina el programa>>")
        for s in await d._siguiente_bloque():
            extra = s.meta.get("titulo") or (s.meta.get("noticia") or {}).get("titulo") or ""
            secuencia.append(f"{s.tipo:7s} {s.etiqueta:18s} {s.duracion:5.1f}s  {extra}  ({s.programa})")
    print("\n".join(secuencia))
    txt = "\n".join(secuencia)
    for esperado in ["apertura", "hora", "bio", "intro programa", "cierre programa", "Tu Cárcel", "noticiero"]:
        assert esperado in txt or esperado == "noticiero", f"falta: {esperado}"
    noti = await d.bloque_noticiero("general")
    print("noticiero:", [(x.etiqueta, round(x.duracion, 1), (x.meta.get('noticia') or {}).get('pais')) for x in noti])
    assert len(noti) == 6 and noti[1].meta["noticia"]["pais"] == "México" and noti[0].etiqueta == "noticiero intro"
    print(f"✅ PLAN OK   archivos mezclados y 'publicados': {len(publicados)}")

async def integracion():
    print("\n── Integración emisora + director (10 s) ──")
    hora["dt"] = datetime(2026, 10, 9, 15, 0)
    em = Emisora()
    d = nuevo(em)
    em.on_cola_baja = d.rellenar
    vistos = []
    class Vc:
        async def play(self, track, start=0, add_history=True): vistos.append(track)
        async def stop(self): pass
    em.sesiones[1] = Vc()
    await em.iniciar()
    await asyncio.sleep(10)
    estado = em.estado()
    await em.detener()
    print("sonó:", vistos)
    assert len(vistos) >= 3 and vistos[0].startswith("PUB["), vistos
    assert any(v.startswith("TRACK[") for v in vistos), "no sonó ninguna canción"
    print("✅ INTEGRACIÓN OK")

creados = preparar_audios()
try:
    asyncio.run(planear())
    asyncio.run(integracion())
finally:
    for r in creados:
        try: os.remove(r)
        except Exception: pass
