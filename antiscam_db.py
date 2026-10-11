import sqlite3
import os
from pathlib import Path

class AntiScamDatabase:
    """BD separada para gestión de antiscam por servidor"""
    
    def __init__(self, db_path: str = "data/antiscam.db"):
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()
    
    def get_connection(self):
        """Obtener conexión a la BD"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def init_db(self):
        """Inicializar tablas de la BD"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Tabla de mensajes recientes por servidor
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS recent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                signature TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                UNIQUE(guild_id, user_id, signature, channel_id, timestamp)
            )
        ''')
        
        # Tabla de firmas marcadas por usuario/servidor
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS flagged_signatures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                signature TEXT NOT NULL,
                flagged_at REAL NOT NULL,
                UNIQUE(guild_id, user_id, signature)
            )
        ''')
        
        # Tabla de verificaciones pendientes
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_verifications (
                user_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                original_nick TEXT,
                created_at REAL NOT NULL
            )
        ''')
        
        # Tabla de alertas activas (para evitar duplicados)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_alerts (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                triggered_at REAL NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def add_recent_message(self, guild_id: int, user_id: int, signature: str, channel_id: int, timestamp: float):
        """Añadir mensaje reciente"""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO recent_messages (guild_id, user_id, signature, channel_id, timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', (guild_id, user_id, signature, channel_id, timestamp))
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        finally:
            conn.close()
    
    def get_recent_messages(self, guild_id: int, user_id: int, signature: str, seconds: int = 25):
        """Obtener mensajes recientes de un usuario/firma en un servidor"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM recent_messages
            WHERE guild_id = ? AND user_id = ? AND signature = ?
            AND timestamp > (SELECT MAX(timestamp) FROM recent_messages 
                WHERE guild_id = ? AND user_id = ? AND signature = ?) - ?
        ''', (guild_id, user_id, signature, guild_id, user_id, signature, seconds))
        
        rows = cursor.fetchall()
        conn.close()
        return rows
    
    def get_unique_channels(self, guild_id: int, user_id: int, signature: str, seconds: int = 25):
        """Contar canales únicos donde se envió la firma en los últimos X segundos"""
        rows = self.get_recent_messages(guild_id, user_id, signature, seconds)
        unique_channels = set(row['channel_id'] for row in rows)
        return unique_channels
    
    def add_flagged_signature(self, guild_id: int, user_id: int, signature: str, timestamp: float):
        """Marcar una firma como spam"""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO flagged_signatures (guild_id, user_id, signature, flagged_at)
                VALUES (?, ?, ?, ?)
            ''', (guild_id, user_id, signature, timestamp))
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        finally:
            conn.close()
    
    def is_flagged_signature(self, guild_id: int, user_id: int, signature: str) -> bool:
        """Verificar si una firma está marcada"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT 1 FROM flagged_signatures
            WHERE guild_id = ? AND user_id = ? AND signature = ?
        ''', (guild_id, user_id, signature))
        
        result = cursor.fetchone()
        conn.close()
        return result is not None
    
    def get_flagged_signatures(self, guild_id: int, user_id: int):
        """Obtener todas las firmas marcadas de un usuario en un servidor"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT signature FROM flagged_signatures
            WHERE guild_id = ? AND user_id = ?
        ''', (guild_id, user_id))
        
        rows = cursor.fetchall()
        conn.close()
        return {row['signature'] for row in rows}
    
    def add_pending_verification(self, user_id: int, guild_id: int, code: str, original_nick: str, timestamp: float):
        """Añadir verificación pendiente"""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT OR REPLACE INTO pending_verifications 
                (user_id, guild_id, code, original_nick, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, guild_id, code, original_nick, timestamp))
            conn.commit()
        finally:
            conn.close()
    
    def get_pending_verification(self, user_id: int):
        """Obtener verificación pendiente de un usuario"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM pending_verifications WHERE user_id = ?
        ''', (user_id,))
        
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    
    def remove_pending_verification(self, user_id: int):
        """Remover verificación pendiente"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM pending_verifications WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
    
    def add_active_alert(self, guild_id: int, user_id: int, timestamp: float):
        """Marcar alerta activa para un usuario"""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT OR REPLACE INTO active_alerts (guild_id, user_id, triggered_at)
                VALUES (?, ?, ?)
            ''', (guild_id, user_id, timestamp))
            conn.commit()
        finally:
            conn.close()
    
    def is_alert_active(self, guild_id: int, user_id: int) -> bool:
        """Verificar si hay una alerta activa"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT 1 FROM active_alerts WHERE guild_id = ? AND user_id = ?
        ''', (guild_id, user_id))
        
        result = cursor.fetchone()
        conn.close()
        return result is not None
    
    def remove_active_alert(self, guild_id: int, user_id: int):
        """Remover alerta activa"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM active_alerts WHERE guild_id = ? AND user_id = ?
        ''', (guild_id, user_id))
        conn.commit()
        conn.close()
    
    def cleanup_old_messages(self, guild_id: int, user_id: int, signature: str, seconds: int = 25):
        """Limpiar mensajes antiguos"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            DELETE FROM recent_messages
            WHERE guild_id = ? AND user_id = ? AND signature = ?
            AND timestamp <= (SELECT MAX(timestamp) FROM recent_messages 
                WHERE guild_id = ? AND user_id = ? AND signature = ?) - ?
        ''', (guild_id, user_id, signature, guild_id, user_id, signature, seconds))
        
        conn.commit()
        conn.close()

# Instancia global
antiscam_db = AntiScamDatabase()
