# MaxIA — paquete actualizado

Copia las 4 carpetas (`cogs/`, `ia_core/`, `musica_core/`, `radio_core/`) encima de las de tu proyecto (junto a `main.py`),
reemplazando los archivos. **Haz una copia de tu proyecto antes.** No incluye `main.py`, `database.py`, `antiscam_db.py` ni
`utils.py` (no los tengo, no los toqué).

## 1) Slash commands (/comando)
1. Carga el cog nuevo `cogs/slash_sync.py` igual que cargas los demás (donde lista tus extensiones en `main.py`).
2. Necesitas `discord.py` 2.4 o más nuevo: `pip install -U discord.py`.
3. Arranca el bot y, con tu prefijo, corre **`!max sync aqui`** (solo el dueño del bot): los slash aparecen al instante en ese
   servidor. Cuando todo se vea bien, corre **`!max sync`** para subirlos a todos los servidores (puede tardar un rato).
   Si no te sale ninguno, re-invita al bot con el permiso/scope `applications.commands`.
4. Repite `!max sync` cada vez que agregues, quites o cambies comandos.
5. Opcional: `SLASH_AUTO_SYNC=1` en el `.env` sincroniza solo al arrancar.
- `/max <mensaje>` habla con la IA (equivale a `!max <mensaje>`). `!max texto` sigue funcionando igual.
- Los comandos con prefijo no cambian: todos siguen funcionando como antes.

## 2) Personajes y modo loco
- Personalidades: `max` (Maxia, ahora mujer), `neko` (Neko Maxia), `yuri` (Yuri), `sexo` (Maxia Fatal).
  Todas se pueden activar desde cualquier canal. De `sexo`, el texto original completo (el que permite seguir el juego si
  alguien empuja a algo explícito) solo se usa en canales NSFW; en los demás canales usa una versión igual de coqueta
  y atrevida pero sin esa parte (`version_sfw` en `ia_core/perfiles_mixin.py`).
- `!max personaje <modo>` cambia la personalidad. Los personajes que no son `max` salen con su nombre y foto (webhook).
  Necesitan el permiso **Gestionar webhooks**; sin él salen con la cuenta del bot y el nombre en negritas.
- Fotos y nombres, sin tocar código (solo admins): `!max pjfoto yuri https://...` y `!max pjnombre sexo Maxia Fatal`.
  Usa links PERMANENTES (imgur, GitHub raw...): los links de archivos subidos a Discord caducan.
  También se pueden poner en el `.env`: `AVATAR_NEKO`, `AVATAR_SEXO`, `AVATAR_YURI`.
- `!max personajes` muestra el elenco. `!max loco on|off` (admins) enciende el modo loco; requiere `!max participar on`.
- Frenos anti-bucle del modo loco (todos ajustables en el `.env`): `LOCO_MAX_CADENA` (4), `LOCO_MAX_TURNOS` (2),
  `LOCO_PROB_APORTE` (0.6), `LOCO_PROB_CONTINUAR` (0.7), `LOCO_DECAIMIENTO` (0.5), `LOCO_ESPERA_MIN_S` (6).

## 3) Memoria del chat
`MEMORIA_CHAT_MIN_MENSAJES` (20), `MEMORIA_CHAT_MAX_MENSAJES` (40), `MEMORIA_CHAT_MAX_CHARS_TOTAL` (9000),
`MEMORIA_CHAT_MAX_CHARS_MENSAJE` (700) en el `.env`.

## 4) Radio
- El salto automático de intros habladas (`saltos.py`) quedó apagado por defecto (gasta IA + internet por cada canción
  nueva). La tabla manual `radio_data/saltos_intro.json` sí funciona (ya trae Daft Punk - Giorgio by Moroder).
  Para el modo automático: `RADIO_SALTO_AUTO=1`.
