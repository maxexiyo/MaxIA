"""Programas de Radio Maxia y la parrilla (qué programa va a qué hora)."""
from datetime import timedelta
from pathlib import Path

from . import config as C

EXTENSIONES = (".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".flac", ".webm")

PROGRAMAS = {
    "general": {
        "nombre": "Radio Maxia ",
        "tema": "Programación musical variada, ambiente relajado y buena onda. Música para acompañar el día.",
        "generos": "éxitos de todas las épocas en español e inglés: pop, rock, baladas, música latina, cumbias, k-pop, Reggaetón, electronica, alternativos, etc y algo actual",
        "charla": False,       # False = solo comenta algunas canciones
        "noticias": True,
        "max_palabras": 30,    # cada intervención de Maxia dura ~12 s como máximo (los demás programas: tope general de ~1 min)
        "factor_habla": 0.25,  # multiplica la probabilidad de comentarios/charlas (0.25 = habla 4 veces menos seguido)
        "exitos_pais": "mx",   # mezcla lo más escuchado hoy en México (Apple Music) con las propuestas de la IA
    },
    "leyendas_del_rock": {
        "nombre": "Leyendas del Rock",
        "tema": ("Programa de rock clásico, un homenaje al estilo de los programas de leyendas del rock: "
                 "biografías, datos curiosos y anécdotas de los grupos e intérpretes que escribieron la historia del rock. "
                 "Antes de cada canción Maxia cuenta algo del grupo o de la canción."),
        "generos": "rock clásico de los años 60, 70, 80 y 90 (solo en inglés), grupos y canciones que hicieron época",
        "charla": True,        # True = antes de cada canción cuenta una biografía/anécdota
        "noticias": True,     # durante este programa no hay noticiero
        "busqueda": "mejores canciones rock clásico conocidas años 70 80 90 leyendas",   # qué busca en internet para proponer canciones
        # Rutas SIN extensión, relativas a radio_data/audio/programas/
        "intro": "leyendas_del_rock/intro",            # The Ides of March - Vehicle (instrumental)
        "intro_max_seg": 25,
        "fondos": ["leyendas_del_rock/fondo"],          # Booker T. & the MG's - Green Onions
        "cortinilla": "leyendas_del_rock/cortinilla",   # The Ventures - Pipeline (solo el inicio)
        "cortinilla_max_seg": 9,
    },
    "virales_2016": {
        "nombre": "Virales del 2016",
        "tema": ("Programa nostálgico con los temas que sonaron en todos lados en 2016: "
                 "EDM, future bass, tropical house y pop electrónico. Maxia comenta con buena onda "
                 "datos curiosos y recuerdos de esa época, sin alargarse demasiado."),
        "generos": ("EDM y pop electrónico de 2016: future bass, tropical house, trap EDM, big room y electropop "
                    "(estilo Marshmello, The Chainsmokers, Kygo, Alan Walker, Zedd, Flume, Martin Garrix, Porter Robinson, Skrillex)"),
        "charla": True,       # solo comenta algunas canciones
        "noticias": True,     # sin noticiero durante el programa
        "busqueda": "canciones virales 2016 EDM future bass tropical house",
    },
    "sonidos_de_america": {
        "nombre": "Sonidos de América",
        "tema": ("Un viaje por la música típica y tradicional de los países de Latinoamérica: en cada canción visitamos "
                 "un país distinto (México, Perú, Chile, Argentina, Colombia, Bolivia, Venezuela, Cuba, Ecuador, "
                 "Uruguay, Paraguay, Costa Rica, República Dominicana y más). Maxia cuenta con orgullo y curiosidad "
                 "de qué país es la canción y qué género es, sin inventar datos."),
        "generos": ("música folclórica y típica de cada país latinoamericano, alternando países en cada tanda: "
                    "México (mariachi, ranchera, huapango, son jarocho), Perú (vals criollo, huayno, marinera), "
                    "Chile (cueca, nueva canción chilena), Argentina (chacarera, zamba, folklore, tango), "
                    "Colombia (vallenato, cumbia, bambuco), Bolivia (huayño, morenada), Venezuela (joropo), "
                    "Cuba (son, bolero), Ecuador (pasillo), Uruguay (candombe, milonga), República Dominicana (merengue)"),
        "charla": True,
        "noticias": True,
        "busqueda": "música típica folclórica de cada país de Latinoamérica canciones famosas",
    },
    "cine_y_soundtracks": {
        "nombre": "Cine y Soundtracks",
        "tema": ("Programa de bandas sonoras: las canciones y temas que sonaron en películas famosas. "
                 "Antes de cada canción Maxia dice en qué película sonó y algo breve para recordar la escena o la película, "
                 "sin inventar datos."),
        "generos": ("canciones y temas de películas famosas de todas las épocas, en inglés y español: clásicos del cine, "
                    "películas de Disney y animación, años 80 y 90, películas románticas, de acción y de ciencia ficción "
                    "(solo canciones que de verdad sonaron en la película o fueron su tema oficial)"),
        "charla": True,
        "noticias": True,
        "busqueda": "canciones famosas de películas banda sonora",
    },
    "madrugada": {
        "nombre": "Madrugada Maxia",
        "tema": "Música tranquila para la noche y la madrugada, sin charla: solo canciones para acompañar a quien sigue despierto.",
        "generos": ("baladas suaves, pop y rock tranquilo, música relajante y canciones para la noche, en español e inglés"),
        "charla": False,
        "noticias": False,
        "solo_musica": True,    # sin charla, hora, noticias, jingles ni mensajes: solo canciones
        "al_entrar": ("Es medianoche. Despídete de los oyentes por esta noche, con cariño y un toque de sueño, en máximo 45 palabras: "
                      "diles que la radio sigue toda la madrugada solo con música y que regresas a las seis de la mañana. "
                      "No digas la hora exacta."),
        "al_salir": ("Son las seis de la mañana. Da los buenos días con energía y calidez en máximo 45 palabras: bienvenida al "
                     "nuevo día, a quienes madrugan y a quienes apenas despiertan, y anuncia que arranca la programación del día. "
                     "No digas la hora exacta."),
        "busqueda": "canciones tranquilas para la noche baladas suaves",
    },
    "videojuegos_ost": {
        "nombre": "Gametime",
        "tema": ("Programa de bandas sonoras de videojuegos: los temas que marcaron generaciones. Maxia cuenta de qué juego es "
                 "cada tema, cómo se siente jugarlo y algún dato curioso de su música o su compositor, con cariño de jugadora "
                 "y sin inventar datos."),
        "generos": ("bandas sonoras de los videojuegos más famosos de todas las épocas: Undertale, Super Mario Galaxy y otros "
                    "Mario, The Legend of Zelda, Pokémon, Minecraft, Final Fantasy, Kingdom Hearts, Sonic, Halo, Skyrim, "
                    "The Witcher 3, Persona, Hollow Knight, Celeste, Doom, Tetris, Chrono Trigger, Metroid, Animal Crossing, "
                    "Dark Souls, Red Dead Redemption, Portal, Bioshock (solo temas de soundtrack originales del juego)"),
        "charla": True,
        "noticias": True,
        "busqueda": "mejores soundtracks de videojuegos temas más famosos OST",
    },
    "cumbias": {
        "nombre": "Hora de bailar",
        "tema": ("Una hora de cumbia para bailar y animar la tarde: cumbia mexicana, colombiana, argentina, peruana y sonidera. "
                 "Maxia habla con mucha alegría y buena onda, con comentarios cortos y de vez en cuando un dato del grupo o la canción."),
        "generos": ("cumbias famosas y bailables: cumbia sonidera y mexicana (Los Ángeles Azules, Grupo Cañaveral, Celso Piña, "
                    "Los Askis, Grupo Quintanilla / Selena, Kumbia Kings, Rigo Tovar), colombiana (Aniceto Molina, Los Corraleros "
                    "de Majagual, La 33, Carlos Vives), argentina (Los Palmeras, Damas Gratis, Gilda, Amar Azul), "
                    "peruana (Los Mirlos, Juaneco y su Combo, Grupo 5), chilena y cumbia clásica y algunas populares y famosas que se suelen poner en fiestas: de artistas como pitbull, caballo dorado, etc" ),
        "charla": True,
        "noticias": True,
        "busqueda": "cumbias más famosas bailables clásicos sonideras",
    },
    "romanticas_de_siempre": {
        "nombre": "Románticas de Siempre",
        "tema": ("Programa de música romántica en español, de las baladas que han enamorado a Latinoamérica por generaciones. "
                 "Maxia habla con cariño y cercanía, como dedicando la canción a alguien especial, "
                 "con un dato o recuerdo breve del artista o la canción."),
        "generos": ("baladas y boleros románticos en español de los años 70 a 2000: Luis Miguel, José José, Juan Gabriel, "
                    "Cristian Castro, Marco Antonio Solís, Mijares, Emmanuel, Yuri, Ana Gabriel, Rocío Dúrcal, "
                    "Armando Manzanero, Franco De Vita, Ricardo Montaner, Ricardo Arjona, Chayanne, Alejandro Sanz, "
                    "Camila, Sin Bandera, Reik, Pablo Alborán, Jesse & Joy"),
        "charla": True,       # antes de cada canción: algo breve y cariñoso del artista o la canción
        "noticias": True,    # sin noticiero durante el programa
        "busqueda": "mejores baladas románticas en español clásicas Luis Miguel José José, juangabriel, etc",
    },
}

# (días 0=lunes … 6=domingo, o None = todos | inicio "HH:MM" | fin "HH:MM" | programa)
PARRILLA = [
    (None, "00:00", "06:00", "madrugada"),
    (None, "12:00", "13:00", "sonidos_de_america"),       # solo domingos
    (None, "14:00", "15:00", "cine_y_soundtracks"),       # solo miércoles y sábados
    (None, "15:00", "16:00", "cumbias"),                  # 1 h diaria
    (None, "16:00", "18:00", "virales_2016"),
    (None, "18:00", "20:00", "romanticas_de_siempre"),
    (None, "20:00", "22:00", "leyendas_del_rock"),
    (None, "22:00", "00:00", "videojuegos_ost"),          # solo martes y viernes (2 h) ← movido
]


def programa_para(dt) -> str:
    minutos = dt.hour * 60 + dt.minute
    for dias, ini, fin, clave in PARRILLA:
        if dias is not None and dt.weekday() not in dias:
            continue
        h1, m1 = map(int, ini.split(":"))
        h2, m2 = map(int, fin.split(":"))
        fin_min = (h2 * 60 + m2) or 1440        # un fin "00:00" es la MEDIANOCHE (24:00); antes valía 0 y el bloque 22:00-00:00 nunca salía al aire
        if h1 * 60 + m1 <= minutos < fin_min:
            return clave
    return "general"


def proximo_cambio(dt):
    """Momento exacto (datetime) en que cambia el programa, o None si no hay cambio en 48 h."""
    actual = programa_para(dt)
    for m in range(1, 48 * 60 + 1):
        t = dt + timedelta(minutes=m)
        if programa_para(t) != actual:
            return t.replace(second=0, microsecond=0)
    return None


def buscar_audio(carpeta: Path, nombre: str):
    """Busca <carpeta>/<nombre>.<cualquier extensión de audio>. Devuelve la ruta o None."""
    base = Path(carpeta) / nombre
    for ext in EXTENSIONES:
        p = Path(str(base) + ext)
        if p.exists():
            return str(p)
    return None


def fondos_locales(clave: str) -> dict:
    """{nombre_visible: ruta} de los fondos instrumentales que hay en disco para este programa."""
    res = {}
    for nombre in PROGRAMAS.get(clave, {}).get("fondos", []):
        ruta = buscar_audio(C.DIR_PROGRAMAS, nombre)
        if ruta:
            res[Path(ruta).stem] = ruta
    if res:             # el programa tiene sus propios fondos: no se mezclan con los de la programación normal
        return res
    if C.DIR_FONDOS.exists():
        for p in sorted(C.DIR_FONDOS.iterdir()):
            if p.suffix.lower() in EXTENSIONES:
                res[p.stem] = str(p)
    return res