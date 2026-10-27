"""Arizona Group Finder API 1.0. Run: uvicorn main:app --host 0.0.0.0 --port 8000"""
import os
import re
import sqlite3
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, Header, Query
from pydantic import BaseModel, Field
try:
    from .server_directory import resolve, directory
except ImportError:
    from server_directory import resolve, directory

DB = Path(os.getenv('AGF_DATABASE', str(Path(__file__).with_name('groupfinder.sqlite3'))))
TTL = 60
NICK = re.compile(r'^[A-Za-z0-9_]{3,24}$')
app = FastAPI(title='Arizona Group Finder', version='1.0.0')

@contextmanager
def connect():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()

@app.on_event('startup')
def init():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('''CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY, created INTEGER NOT NULL, last_seen INTEGER NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS listings (
          token TEXT PRIMARY KEY REFERENCES sessions(token), nickname TEXT NOT NULL,
          player_id INTEGER NOT NULL, game_server TEXT NOT NULL, job TEXT NOT NULL,
          kind TEXT NOT NULL, members INTEGER NOT NULL, updated INTEGER NOT NULL)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_listings_server ON listings(game_server,updated)')

class SessionResponse(BaseModel):
    token: str

class ListingInput(BaseModel):
    nickname: str = Field(min_length=3, max_length=24)
    player_id: int = Field(ge=0, le=1004)
    game_server: str = Field(min_length=3, max_length=253)
    job: Literal['taxi','trucker','miner','farmer','other']
    kind: Literal['looking','recruiting']
    members: int = Field(ge=1, le=5)

    def check(self):
        if not NICK.fullmatch(self.nickname):
            raise HTTPException(422, 'Invalid nickname')
        if not re.fullmatch(r'[a-zA-Z0-9.:-]{3,253}', self.game_server):
            raise HTTPException(422, 'Invalid server address')
        if self.kind == 'looking' and self.members != 1:
            raise HTTPException(422, 'Looking players must have members=1')
        if self.kind == 'recruiting' and self.members >= 5:
            raise HTTPException(422, 'Group already full')

def cleanup(c, now):
    c.execute('DELETE FROM listings WHERE updated < ?', (now-TTL,))
    c.execute('DELETE FROM sessions WHERE last_seen < ?', (now-86400,))

def auth(c, token):
    if not token or not token.startswith('Bearer '):
        raise HTTPException(401, 'Missing token')
    key = token[7:]
    if not c.execute('SELECT 1 FROM sessions WHERE token=?',(key,)).fetchone():
        raise HTTPException(401, 'Invalid session')
    return key

@app.get('/api/v1/health')
def health():
    return {'status':'ok','version':'1.0.0'}

@app.get('/api/v1/servers/resolve')
def resolve_server(address: str = Query(min_length=3,max_length=253), port: int = Query(ge=1,le=65535)):
    record,error=resolve(address,port)
    if record is None:
        raise HTTPException(503 if not directory() else 404, detail=error or 'unknown_server')
    return {'server_id':record['key'],'server_name':record['name'], 'host':record['host'],'port':record['port']}

@app.post('/api/v1/session', response_model=SessionResponse)
def session():
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with connect() as c:
        cleanup(c, now)
        c.execute('INSERT INTO sessions VALUES (?,?,?)', (token,now,now))
    return {'token': token}

@app.post('/api/v1/listings')
def publish(payload: ListingInput, authorization: str|None=Header(default=None)):
    payload.check()
    # A listed server ID must exist in the trusted directory; clients cannot invent one.
    records=directory()
    if payload.game_server not in {r['key'] for r in records}:
        raise HTTPException(422, 'Unknown Arizona server ID')
    now = int(time.time())
    with connect() as c:
        key = auth(c, authorization)
        cleanup(c, now)
        c.execute('''INSERT INTO listings VALUES (?,?,?,?,?,?,?,?)
          ON CONFLICT(token) DO UPDATE SET nickname=excluded.nickname,
          player_id=excluded.player_id, game_server=excluded.game_server,
          job=excluded.job, kind=excluded.kind, members=excluded.members, updated=excluded.updated''',
          (key,payload.nickname,payload.player_id,payload.game_server,payload.job,payload.kind,payload.members,now))
    return {'ok': True}

@app.get('/api/v1/listings')
def listings(game_server: str = Query(min_length=3,max_length=100), job: str|None=None):
    if not re.fullmatch(r'[a-zA-Z0-9.:-]{3,100}', game_server):
        raise HTTPException(422,'Invalid server')
    if job and job not in ('taxi','trucker','miner','farmer','other'):
        raise HTTPException(422,'Invalid job')
    with connect() as c:
        cleanup(c, int(time.time()))
        if job:
            rows = c.execute('''SELECT nickname,player_id,game_server,job,kind,members,updated
              FROM listings WHERE game_server=? AND job=? ORDER BY updated DESC LIMIT 100''',(game_server,job)).fetchall()
        else:
            rows = c.execute('''SELECT nickname,player_id,game_server,job,kind,members,updated
              FROM listings WHERE game_server=? ORDER BY updated DESC LIMIT 100''',(game_server,)).fetchall()
    return {'listings':[dict(r) for r in rows]}

@app.post('/api/v1/heartbeat')
def heartbeat(authorization: str|None=Header(default=None)):
    now=int(time.time())
    with connect() as c:
        key=auth(c,authorization)
        cleanup(c,now)
        c.execute('UPDATE sessions SET last_seen=? WHERE token=?',(now,key))
        res=c.execute('UPDATE listings SET updated=? WHERE token=?',(now,key))
    return {'ok': True,'active':res.rowcount>0}

@app.delete('/api/v1/listings/me')
def remove(authorization: str|None=Header(default=None)):
    with connect() as c:
        key=auth(c,authorization)
        c.execute('DELETE FROM listings WHERE token=?',(key,))
    return {'ok':True}
