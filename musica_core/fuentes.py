import asyncio
import logging
import wavelink

from musica_core.nodos import buscar_tracks_con_fallback

logger = logging.getLogger(__name__)


class YTMusicSource:
    """Fuente de audio usando Wavelink/Lavalink (sin yt-dlp ni ytmusicapi)"""
    def __init__(self, *, data, volume=0.5):
        self.data = data
        self.title = data.get('title', 'Desconocido')
        self.url = data.get('url', '')
        self.duration = data.get('duration', 0)
        self.thumbnail = data.get('thumbnail', None)
        self.uploader = data.get('uploader', 'Artista Desconocido')

    @classmethod
    async def resolve_playlist(cls, ctx, query: str, loop=None):
        """Revisa si la query/link es una PLAYLIST completa (vía Lavalink: YouTube, YT Music,
        Spotify si hay plugin, etc). Si lo es, devuelve (nombre_playlist, lista_de_tracks) con
        TODAS las canciones de la playlist. Si no es una playlist (o no se pudo resolver),
        devuelve None y el llamador debe seguir con la búsqueda normal."""
        loop = loop or asyncio.get_event_loop()
        try:
            wl_results = await buscar_tracks_con_fallback(query)
        except Exception as e:
            logger.warning(f"Error revisando si '{query}' es una playlist: {e}")
            return None

        if not wl_results or not isinstance(wl_results, wavelink.Playlist):
            return None

        playlist_name = wl_results.name or "Playlist sin nombre"
        tracks = []
        for entry in wl_results.tracks:
            tracks.append({
                'title': entry.title or 'Desconocido',
                'uploader': entry.author or 'Artista Desconocido',
                'duration': (entry.length or 0) // 1000,
                'thumbnail': entry.artwork,
                'url': entry.uri or query,
                'query': query,
                'requester': ctx.author
            })

        if not tracks:
            return None

        return playlist_name, tracks

    @classmethod
    async def create_sources_from_query(cls, ctx, query: str, loop=None, limit=15):
        """Busca canciones usando Wavelink/Lavalink. Si es una playlist, la resuelve; si es
        texto plano, lo pasa a ytmsearch: para buscar en YouTube Music."""
        loop = loop or asyncio.get_event_loop()
        tracks = []
        is_url = query.startswith('http://') or query.startswith('https://')
        original_query = query

        try:
            # ✅ Failsafe: si es texto plano, le ponemos el prefijo ytmsearch: para que Lavalink
            # sepa que es una búsqueda de YouTube Music (audio limpio, sin el video oficial).
            if not is_url and ':' not in query.split(' ')[0]:
                query = f"ytmsearch:{query}"

            wl_results = await buscar_tracks_con_fallback(query)
            if not wl_results:
                return tracks

            if isinstance(wl_results, wavelink.Playlist):
                entries = wl_results.tracks
            else:
                entries = wl_results

            for entry in entries:
                tracks.append({
                    'title': entry.title or 'Desconocido',
                    'uploader': entry.author or 'Artista Desconocido',
                    'duration': (entry.length or 0) // 1000,
                    'thumbnail': entry.artwork,
                    'url': entry.uri or f"ytsearch:{original_query}",
                    'query': original_query,
                    'requester': ctx.author
                })
        except Exception as e:
            logger.error(f"Error al extraer fuentes: {e}")

        if limit == 1 and len(tracks) > 1:
            return tracks[:1]

        return tracks
