"""
Arma con ffmpeg los SEGMENTOS HABLADOS de la radio: un solo archivo mp3 que junta
  · una intro / jingle / cortinilla (opcional)
  · la voz de Maxia (opcional), normalizada de volumen
  · un fondo instrumental en bucle (opcional)
  · una música de cierre (outro, opcional) que entra justo cuando la voz termina, PEGADA en el mismo archivo
    (así no hay corte entre la voz y la musiquita final)
con ducking de verdad (sidechaincompress): cuando Maxia habla, lo demás baja solo.
Ese mp3 se sube a Discord y Lavalink lo reproduce como cualquier otro track.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from . import config as C

logger = logging.getLogger(__name__)


@dataclass
class Audio:
    ruta: str
    max_seg: Optional[float] = None     # recorta a esta duración (con fade out)
    voz_espera: bool = False            # True = la voz entra hasta que este audio termina por completo (cortinillas)


async def duracion(ruta: str) -> float:
    """Duración en segundos de un archivo de audio (0.0 si no se pudo leer)."""
    try:
        p = await asyncio.create_subprocess_exec(
            C.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", ruta,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=15)
        return float(out.decode().strip() or 0)
    except Exception as e:
        logger.warning(f"[mezcla] no pude medir {ruta}: {e!r}")
        return 0.0


def _grafo(voz, inicio, fondo, idx, delay, total, normalizar_amix: bool, dv: float = 0.0,
           outro=None, t_outro: float = 0.0) -> str:
    partes, mezcla = [], []
    n_sc = (1 if inicio else 0) + (1 if fondo else 0)

    if voz:
        c = (f"[{idx['v']}:a]aresample=48000,aformat=channel_layouts=stereo,"
             f"loudnorm=I={C.VOZ_LUFS}:TP=-1.5:LRA=11,aresample=48000")
        if delay > 0:
            ms = int(delay * 1000)
            c += f",adelay={ms}|{ms}"
        c += f",apad=whole_dur={total:.2f}"     # la voz dura todo el segmento (si no, el sidechain corta el fondo al terminar)
        salidas = ["vmix"] + [f"vsc{k}" for k in range(n_sc)]
        c += f",asplit={len(salidas)}" + "".join(f"[{s}]" for s in salidas)
        partes.append(c)
        mezcla.append("[vmix]")

    sc = 0
    if inicio:
        di = inicio[1]
        c = f"[{idx['i']}:a]aresample=48000,aformat=channel_layouts=stereo,volume={C.GAN_INICIO}"
        if di > 2.0:
            c += f",afade=t=out:st={max(0.0, di - 1.5):.2f}:d=1.5"
        if voz:
            partes.append(c + "[ini]")
            partes.append(f"[ini][vsc{sc}]sidechaincompress=threshold=0.02:ratio=4:attack=30:release=600:makeup=1[inid]")
            sc += 1
            mezcla.append("[inid]")
        else:
            partes.append(c + "[inid]")
            mezcla.append("[inid]")

    if fondo:
        c = (f"[{idx['f']}:a]aresample=48000,aformat=channel_layouts=stereo,volume={C.GAN_FONDO},"
             f"afade=t=in:d=0.5")
        if inicio:      # el fondo entra cuando la intro/jingle ya casi termina (antes sonaban las dos músicas a la vez)
            ms = int(max(0.0, inicio[1] - 1.5) * 1000)
            if ms > 0:
                c += f",adelay={ms}|{ms}"
        if outro and voz:   # el fondo se apaga cuando termina la voz de cierre, justo antes de que entre la música final
            c += f",afade=t=out:st={delay + dv + 0.25:.2f}:d=1.2"
        if voz:
            partes.append(c + "[bg]")
            partes.append(f"[bg][vsc{sc}]sidechaincompress=threshold=0.02:ratio=5:attack=30:release=700:makeup=1[bgd]")
            mezcla.append("[bgd]")
        else:
            partes.append(c + "[bgd]")
            mezcla.append("[bgd]")

    if outro and voz:
        do = outro[1]
        c = f"[{idx['o']}:a]aresample=48000,aformat=channel_layouts=stereo,volume={C.GAN_INICIO}"
        if do > 2.0:
            c += f",afade=t=out:st={max(0.0, do - 1.5):.2f}:d=1.5"
        ms = int(t_outro * 1000)
        if ms > 0:
            c += f",adelay={ms}|{ms}"
        partes.append(c + "[fin]")
        mezcla.append("[fin]")

    k = len(mezcla)
    final = "".join(mezcla)
    if k > 1:
        final += f"amix=inputs={k}:duration=longest" + (":normalize=0" if normalizar_amix else "")
        if not normalizar_amix:
            final += f",volume={k}"
    else:
        final += "anull"
    final += ",alimiter=limit=0.95"
    if voz and outro:
        pass        # el fade de salida es el de la propia música final
    elif voz:
        # el fade de salida empieza DESPUÉS de que Maxia termina de hablar (antes se comía el final de la frase)
        st = delay + dv + 0.25
        if total - st > 0.2:
            final += f",afade=t=out:st={st:.2f}:d={total - st:.2f}"
    elif total > 2.0:
        final += f",afade=t=out:st={total - 1.5:.2f}:d=1.5"
    partes.append(final + "[out]")
    return ";".join(partes)


async def armar_segmento(voz: Optional[str], *, inicio: Optional[Audio] = None, fondo: Optional[str] = None,
                         salida: str, voz_tras_intro: bool = False, outro: Optional[Audio] = None) -> float:
    """Genera `salida` (mp3) y devuelve su duración en segundos. Lanza RuntimeError si ffmpeg falla."""
    if not voz and not inicio:
        raise RuntimeError("un segmento necesita voz o intro")
    dv = await duracion(voz) if voz else 0.0
    di = 0.0
    if inicio:
        di = await duracion(inicio.ruta)
        if inicio.max_seg:
            di = min(di, inicio.max_seg)
        if di <= 0:
            inicio = None
    do = 0.0
    if outro and voz:
        do = await duracion(outro.ruta)
        if outro.max_seg:
            do = min(do, outro.max_seg)
    if do <= 0:
        outro = None        # sin voz o sin música válida: se arma como siempre
    if voz and dv <= 0:
        raise RuntimeError("la voz está vacía")
    if not voz and not inicio:
        raise RuntimeError("no hay audio que armar")

    if voz and inicio and inicio.voz_espera:    # cortinilla: suena SOLA y completa; Maxia habla hasta que termina
        delay = di + 0.2
    elif voz and inicio and voz_tras_intro:     # la intro suena SOLA completa; Maxia habla cuando entra el fondo
        delay = max(0.0, di - 1.5)
    else:
        delay = min(C.INTRO_SOLO_MAX_S, di * 0.5) if (voz and inicio) else 0.0
    if voz and (fondo or inicio):
        total = max(delay + dv + C.COLA_FONDO_S, di)
    elif voz:
        total = dv + 0.9            # colchón al final: el codificador mp3 y Lavalink recortan un poco
    else:
        total = di
    t_outro = 0.0
    if outro:                    # la música final entra 0.6 s después de que termina la voz y dura hasta el final
        t_outro = delay + dv + 0.6
        total = max(t_outro + do, di)

    cmd = [C.FFMPEG, "-y", "-nostdin", "-loglevel", "error"]
    idx, n = {}, 0
    if voz:
        cmd += ["-i", voz]
        idx["v"] = n
        n += 1
    if inicio:
        cmd += ["-t", f"{di:.2f}", "-i", inicio.ruta]
        idx["i"] = n
        n += 1
    if fondo:
        cmd += ["-stream_loop", "-1", "-i", fondo]
        idx["f"] = n
        n += 1
    if outro:
        cmd += ["-t", f"{do:.2f}", "-i", outro.ruta]
        idx["o"] = n
        n += 1

    ultimo_error = ""
    for normalizar in (True, False):          # ffmpeg viejo no conoce amix normalize=0
        grafo = _grafo(voz, (inicio.ruta, di) if inicio else None, fondo, idx, delay, total, normalizar, dv,
                       (outro.ruta, do) if outro else None, t_outro)
        args = cmd + ["-filter_complex", grafo, "-map", "[out]", "-t", f"{total:.2f}",
                      "-ac", "2", "-ar", "48000", "-c:a", "libmp3lame", "-b:a", "128k", salida]
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(p.communicate(), timeout=120)
        except asyncio.TimeoutError:
            p.kill()
            await p.wait()          # sin esto el proceso muerto quedaba como zombie
            raise RuntimeError("ffmpeg tardó demasiado armando el segmento")
        if p.returncode == 0:
            real = await duracion(salida)
            return real or total
        ultimo_error = err.decode(errors="replace")[-400:]
    raise RuntimeError(f"ffmpeg falló: {ultimo_error}")