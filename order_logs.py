"""Disk-backed order messages; bounded pages keep long-running dashboards small."""
from contextlib import closing
from pathlib import Path
import sqlite3

PATH = Path(__file__).resolve().parent / 'order_status.sqlite3'


def _connect():
    connection = sqlite3.connect(PATH, timeout=5)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS order_logs (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, message TEXT NOT NULL)')
    except BaseException:
        connection.close()
        raise
    return connection


def append(timestamp, message):
    with closing(_connect()) as connection, connection:
        cursor=connection.execute('INSERT INTO order_logs(timestamp,message) VALUES (?,?)',(timestamp,message))
        return cursor.lastrowid


def page(before=None, limit=200):
    limit=max(1,min(200,limit))
    with closing(_connect()) as connection:
        if before is None:
            rows=connection.execute('SELECT id,timestamp,message FROM order_logs ORDER BY id DESC LIMIT ?',(limit+1,)).fetchall()
        else:
            rows=connection.execute('SELECT id,timestamp,message FROM order_logs WHERE id < ? ORDER BY id DESC LIMIT ?',(before,limit+1)).fetchall()
    return {'logs':[dict(zip(('id','timestamp','message'),row)) for row in rows[:limit]],
            'has_more':len(rows)>limit}
