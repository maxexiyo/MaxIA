"""La hora dicha como en la radio ("son las ocho y cuarto de la noche, horario de México")."""
_NUM = {1: "una", 2: "dos", 3: "tres", 4: "cuatro", 5: "cinco", 6: "seis", 7: "siete",
        8: "ocho", 9: "nueve", 10: "diez", 11: "once", 12: "doce"}


def _periodo(h: int) -> str:
    if h < 6:
        return "de la madrugada"
    if h < 12:
        return "de la mañana"
    if h < 19:
        return "de la tarde"
    return "de la noche"


def hora_hablada(dt) -> str:
    h, m = dt.hour, dt.minute
    if m == 0 and h == 12:
        return "las doce del día, en punto"
    if m == 0 and h == 0:
        return "las doce de la noche, en punto"
    base = (h + 1) % 24 if m == 45 else h          # "ocho menos cuarto" usa la hora siguiente
    h12 = base % 12 or 12
    art = "la" if h12 == 1 else "las"
    if m == 0:
        cola = "en punto"
    elif m == 15:
        cola = "y cuarto"
    elif m == 30:
        cola = "y media"
    elif m == 45:
        cola = "menos cuarto"
    else:
        cola = f"con {m} minutos" if m != 1 else "con un minuto"
    return f"{art} {_NUM[h12]} {cola} {_periodo(h)}"


def verbo_hora(dt) -> str:
    """'Es' solo cuando la hora dicha es la una ('es la una y cuarto'); 'Son' en los demás casos."""
    h, m = dt.hour, dt.minute
    base = (h + 1) % 24 if m == 45 else h
    return "Es" if (base % 12 or 12) == 1 else "Son"


def anuncio_hora(dt) -> str:
    """Frase exacta para el aire: 'Son las ocho y cuarto de la noche, horario de México.'"""
    return f"{verbo_hora(dt)} {hora_hablada(dt)}, horario de México."


def anuncios_hora(dt) -> list:
    """Las 2 variantes de la frase de la hora (cada una se guarda como audio aparte en voces_fijas/)."""
    return [
        anuncio_hora(dt),
        f"Radio Maxia te lo recuerda: {verbo_hora(dt).lower()} {hora_hablada(dt)}.",
    ]