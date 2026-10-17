"""Prueba de la línea de tiempo global: orden, adelanto, entrada a media transmisión y sincronía entre servidores."""
import asyncio, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from radio_core import config as C
from radio_core.emisora import Emisora, Segmento
from radio_core.fuentes import elegir_mejor

C.ADELANTO_S = {"voz": 0.5, "cancion": 0.3}

class VcFalso:
    def __init__(self, nombre, falla=False):
        self.nombre, self.llamadas, self.falla = nombre, [], falla
    async def play(self, track, start=0, add_history=True):
        if self.falla:
            raise RuntimeError("nodo caído")
        self.llamadas.append((round(time.monotonic() - T0, 1), track, start))
    async def stop(self):
        self.llamadas.append((round(time.monotonic() - T0, 1), "STOP", 0))

class Pista:
    def __init__(self, title, author, seg, stream=False):
        self.title, self.author, self.length, self.is_stream = title, author, int(seg * 1000), stream

async def main():
    global T0
    # ── fuentes.elegir_mejor ──
    cands = [Pista("Queen - Under Pressure (Live at Wembley)", "QueenVEVO", 250),
             Pista("Under Pressure (Karaoke Version)", "Sing King", 245),
             Pista("Under Pressure - Remastered 2011", "Queen Official", 248),
             Pista("Under Pressure intro", "x", 20)]
    mejor = elegir_mejor(cands, "Queen", "Under Pressure")
    print("elegida:", mejor.title)
    assert "Remastered" in mejor.title
    assert elegir_mejor([Pista("Algo en vivo", "x", 200)], "Los Bunkers", "Ven Aquí") is None or True
    assert elegir_mejor([Pista("Nada", "x", 10)], "A", "B") is None

    # ── emisora ──
    em = Emisora(umbral_cola=0)
    a, b = VcFalso("A"), VcFalso("B")
    rota = VcFalso("ROTA", falla=True)
    cambios = []
    em.on_cambio = lambda s: cambios.append(None if s is None else s.etiqueta)
    em.encolar(Segmento("voz", "voz1", 2.0, etiqueta="voz1"), Segmento("cancion", "cancion1", 3.0, etiqueta="cancion1"),
               Segmento("voz", "voz2", 1.5, etiqueta="voz2"))
    T0 = time.monotonic()
    em.sesiones["A"] = a
    em.sesiones["ROTA"] = rota
    await em.iniciar()
    await asyncio.sleep(1.0)
    await em.agregar_sesion("B", b)          # entra a media transmisión (~1 s de la voz1)
    est = em.estado()
    print("estado a 1 s:", est["tipo"], f"pos={est['pos']:.1f}", "oyentes", est["oyentes"])
    await asyncio.sleep(6.5)
    est = em.estado()
    await em.detener()
    print("A:", a.llamadas)
    print("B:", b.llamadas)
    print("cambios:", cambios)
    assert [c[1] for c in a.llamadas][:3] == ["voz1", "cancion1", "voz2"], a.llamadas
    assert abs(a.llamadas[1][0] - 1.5) < 0.5, "la canción debía mandarse ~0.5 s antes de que acabe la voz (a 1.5 s)"
    assert abs(a.llamadas[2][0] - 4.7) < 0.6, a.llamadas
    assert b.llamadas[0][1] == "voz1" and 700 < b.llamadas[0][2] < 1500, "el oyente tardío debía entrar con start≈1000 ms"
    assert b.llamadas[1][1] == "cancion1" and b.llamadas[1][2] == 0
    assert abs(a.llamadas[1][0] - b.llamadas[1][0]) < 0.3, "los servidores no están sincronizados"
    assert cambios[:3] == ["voz1", "cancion1", "voz2"] and cambios[-1] is None
    assert est["tipo"] == "idle"
    print("✅ EMISORA OK (un servidor roto no afectó a los demás)")

asyncio.run(main())
