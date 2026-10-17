import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
import json
import logging

logger = logging.getLogger(__name__)

DB_FOLDER = Path(__file__).parent / 'database'
DB_FOLDER.mkdir(exist_ok=True)

class DatabaseManager:
    """Gestor centralizado de base de datos por servidor"""
    
    def __init__(self):
        self.db_folder = DB_FOLDER
        self.connections = {}
    
    def get_db_path(self, guild_id: int):
        """Obtener ruta de BD para un servidor"""
        return self.db_folder / f'guild_{guild_id}.db'
    
    def get_connection(self, guild_id: int):
        """Obtener conexión a BD (cacheada)"""
        if guild_id not in self.connections:
            db_path = self.get_db_path(guild_id)
            self.connections[guild_id] = sqlite3.connect(str(db_path))
            self.connections[guild_id].row_factory = sqlite3.Row
            self._init_db(guild_id)
        return self.connections[guild_id]
    
    def _init_db(self, guild_id: int):
        """Inicializar tablas de una BD"""
        conn = self.connections[guild_id]
        cursor = conn.cursor()
        
        # Tabla de configuración
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        
        # Tabla de warns
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS warns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                mod_id INTEGER,
                reason TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Tabla de auditoría
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT,
                user_id INTEGER,
                mod_id INTEGER,
                details TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Tabla de cerebro Markov
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS markov_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                word TEXT,
                next_word TEXT,
                count INTEGER DEFAULT 1
            )
        ''')
        
        # Tabla de reaction roles
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reaction_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER UNIQUE,
                reactions TEXT,
                roles TEXT
            )
        ''')
        
        # Tabla de suspensiones (mutes)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS mutes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE,
                moderator_id INTEGER,
                reason TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Tabla de bans
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE,
                moderator_id INTEGER,
                reason TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        logger.info(f'✅ BD inicializada para servidor {guild_id}')
    
    def get_config(self, guild_id: int, key: str, default=None):
        """Obtener valor de configuración"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM config WHERE key = ?', (key,))
        result = cursor.fetchone()
        return result[0] if result else default
    
    def set_config(self, guild_id: int, key: str, value):
        """Guardar valor de configuración"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)', (key, str(value)))
        conn.commit()
    
    def get_all_config(self, guild_id: int):
        """Obtener toda la configuración"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        cursor.execute('SELECT key, value FROM config')
        return dict(cursor.fetchall())
    
    def add_warn(self, guild_id: int, user_id: int, mod_id: int, reason: str):
        """Añadir aviso a usuario"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO warns (user_id, mod_id, reason) VALUES (?, ?, ?)',
            (user_id, mod_id, reason)
        )
        conn.commit()
        return cursor.lastrowid
    
    def get_warns(self, guild_id: int, user_id: int):
        """Obtener warns de usuario"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM warns WHERE user_id = ?', (user_id,))
        return cursor.fetchall()
    
    def get_warn_count(self, guild_id: int, user_id: int):
        """Obtener cantidad de warns"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM warns WHERE user_id = ?', (user_id,))
        return cursor.fetchone()[0]
    
    def delete_warns(self, guild_id: int, user_id: int):
        """Borrar warns de usuario"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM warns WHERE user_id = ?', (user_id,))
        conn.commit()
    
    def add_audit_log(self, guild_id: int, action: str, user_id: int, mod_id: int, details: str = ''):
        """Añadir entrada de auditoría"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO audit_log (action, user_id, mod_id, details) VALUES (?, ?, ?, ?)',
            (action, user_id, mod_id, details)
        )
        conn.commit()
    
    def get_audit_logs(self, guild_id: int, limit: int = 50):
        """Obtener logs de auditoría"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?', (limit,))
        return cursor.fetchall()
    
    def cleanup_old_audit_logs(self, guild_id: int, days: int = 180):
        """Limpiar logs de auditoría más antiguos de X días (solo si hay >20)"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        
        # Contar registros totales
        cursor.execute('SELECT COUNT(*) FROM audit_log')
        count = cursor.fetchone()[0]
        
        if count > 20:
            cutoff_date = datetime.now() - timedelta(days=days)
            cursor.execute(
                'DELETE FROM audit_log WHERE timestamp < ?',
                (cutoff_date,)
            )
            conn.commit()
            logger.info(f'🧹 Limpiados {cursor.rowcount} logs antiguos del servidor {guild_id}')
    
    def add_markov_word(self, guild_id: int, user_id: int, word: str, next_word: str):
        """Añadir palabra al cerebro Markov"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        
        cursor.execute(
            'SELECT COUNT(*) FROM markov_data WHERE word = ? AND next_word = ?',
            (word, next_word)
        )
        
        if cursor.fetchone()[0] > 0:
            cursor.execute(
                'UPDATE markov_data SET count = count + 1 WHERE word = ? AND next_word = ?',
                (word, next_word)
            )
        else:
            cursor.execute(
                'INSERT INTO markov_data (user_id, word, next_word) VALUES (?, ?, ?)',
                (user_id, word, next_word)
            )
        
        conn.commit()
    
    def get_markov_chain(self, guild_id: int, word: str):
        """Obtener palabras siguientes disponibles"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        cursor.execute(
            'SELECT next_word, count FROM markov_data WHERE word = ? ORDER BY count DESC',
            (word,)
        )
        return cursor.fetchall()
    
    def clear_markov_data(self, guild_id: int):
        """Borrar datos del cerebro Markov"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM markov_data')
        conn.commit()
    
    def add_reaction_role(self, guild_id: int, message_id: int, reactions: list, roles: list):
        """Añadir reaction role"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        reactions_json = json.dumps(reactions)
        roles_json = json.dumps(roles)
        
        cursor.execute(
            'INSERT OR REPLACE INTO reaction_roles (message_id, reactions, roles) VALUES (?, ?, ?)',
            (message_id, reactions_json, roles_json)
        )
        conn.commit()
    
    def get_reaction_role(self, guild_id: int, message_id: int):
        """Obtener reaction role"""
        conn = self.get_connection(guild_id)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM reaction_roles WHERE message_id = ?', (message_id,))
        result = cursor.fetchone()
        if result:
            return {
                'reactions': json.loads(result[2]),
                'roles': json.loads(result[3])
            }
        return None
    
    def close_all(self):
        """Cerrar todas las conexiones"""
        for conn in self.connections.values():
            conn.close()

# Instancia global
db = DatabaseManager()