"""Prueba de los segmentos hablados: duración, ducking del fondo/intro bajo la voz y fade final (con tonos de prueba)."""
import asyncio, os, subprocess, sys, tempfile
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from radio_core import config as C
from radio_core.mezcla import Audio, armar_segmento, duracion

TMP = tempfile.mkdtemp()

def tono(nombre, hz, seg, vol=0.5):
    ruta = os.path.join(TMP, nombre)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={seg}",
                    "-af", f"volume={vol * 8}", "-ar", "48000", "-ac", "2", ruta], check=True)
    return ruta

def leer(ruta):
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", ruta, "-f", "s16le", "-ar", "48000", "-ac", "1", "pipe:1"],
                         capture_output=True).stdout
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0

def amp(x, a, b, hz):
    x = x[int(a * 48000):int(b * 48000)]
    t = np.arange(len(x)) / 48000
    return 2 * abs(np.mean(x * np.exp(-2j * np.pi * hz * t)))

async def main():
    voz = tono("voz.wav", 880, 6)          # "voz" de 6 s a 880 Hz
    intro = tono("intro.wav", 440, 20)     # intro de 20 s a 440 Hz
    fondo = tono("fondo.wav", 220, 3)      # fondo corto (se repite en bucle) a 220 Hz

    # 1) voz + fondo: el fondo debe bajar mientras habla y subir después, y el archivo debe durar voz + cola
    s1 = os.path.join(TMP, "s1.mp3")
    d1 = await armar_segmento(voz, fondo=fondo, salida=s1)
    x = leer(s1)
    print(f"[voz+fondo] duración {d1:.2f}s (esperado ~{6 + C.COLA_FONDO_S:.1f}s)")
    bg_hablando = amp(x, 2.0, 5.0, 220); bg_libre = amp(x, 6.4, 7.0, 220); v = amp(x, 2.0, 5.0, 880)
    print(f"   fondo hablando {bg_hablando:.3f} | fondo libre {bg_libre:.3f} | voz {v:.3f}")
    assert abs(d1 - (6 + C.COLA_FONDO_S)) < 0.4
    assert bg_hablando < 0.6 * bg_libre, "el fondo no bajó cuando habla la voz (ducking)"
    assert v > 0.05
    assert np.abs(x[-int(0.1 * 48000):]).max() < 0.05, "no hay fade out al final"

    # 2) intro + voz + fondo: la intro suena sola unos segundos y luego baja bajo la voz
    s2 = os.path.join(TMP, "s2.mp3")
    d2 = await armar_segmento(voz, inicio=Audio(intro, 12), fondo=fondo, salida=s2)
    x = leer(s2)
    solo = amp(x, 0.5, 2.5, 440); bajo_voz = amp(x, 5.0, 8.0, 440)
    print(f"[intro+voz+fondo] duración {d2:.2f}s | intro sola {solo:.3f} | intro bajo la voz {bajo_voz:.3f}")
    assert d2 >= 11.5, "la intro de 12 s no se respetó"
    assert bajo_voz < 0.7 * solo, "la intro no bajó bajo la voz"

    # 3) solo jingle recortado a 5 s (sin voz)
    s3 = os.path.join(TMP, "s3.mp3")
    d3 = await armar_segmento(None, inicio=Audio(intro, 5), salida=s3)
    print(f"[solo jingle] duración {d3:.2f}s (esperado ~5s)")
    assert abs(d3 - 5) < 0.4

    # 4) solo voz
    s4 = os.path.join(TMP, "s4.mp3")
    d4 = await armar_segmento(voz, salida=s4)
    print(f"[solo voz] duración {d4:.2f}s (esperado ~6.3s)")
    assert abs(d4 - 6.3) < 0.4
    assert await duracion(s4) > 6
    print("✅ MEZCLA OK")

asyncio.run(main())
