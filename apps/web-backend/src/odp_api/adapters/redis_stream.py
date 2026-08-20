"""Small Redis Stream client plus a durable local substitute for non-Docker development."""

import json
import socket
import sqlite3
import ssl
from collections.abc import Sequence
from pathlib import Path
from threading import RLock
from urllib.parse import unquote, urlparse

_RECOVERABLE_XADD_ONCE = """
local state = redis.call('GET', KEYS[1])
if state == 'published' or state == '1' then return nil end
if not state then
    redis.call('SET', KEYS[1], 'pending', 'NX')
end
local appended = redis.pcall('XADD', KEYS[2], '*', unpack(ARGV))
if type(appended) == 'table' and appended.err then
    return redis.error_reply(appended.err)
end
redis.call('SET', KEYS[1], 'published', 'XX')
return appended
"""


class SQLiteStreamClient:
    """Durable stream command surface used only outside the Redis-backed runtime."""

    def __init__(self, database_path: Path | str) -> None:
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._lock = RLock()
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS stream_entries (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_name TEXT NOT NULL,
                    fields_json TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS stream_idempotency (
                    idempotency_key TEXT PRIMARY KEY
                )
                """
            )

    def xadd(self, stream: str, fields: dict[str, str]) -> str:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT INTO stream_entries (stream_name, fields_json) VALUES (?, ?)",
                (stream, json.dumps(fields, sort_keys=True)),
            )
        return f"{cursor.lastrowid}-0"

    def xadd_once(self, stream: str, claim_key: str, fields: dict[str, str]) -> str | None:
        """Claim and append in one SQLite transaction."""
        with self._lock, self._connection:
            claim = self._connection.execute(
                "INSERT OR IGNORE INTO stream_idempotency (idempotency_key) VALUES (?)", (claim_key,)
            )
            if claim.rowcount != 1:
                return None
            cursor = self._connection.execute(
                "INSERT INTO stream_entries (stream_name, fields_json) VALUES (?, ?)",
                (stream, json.dumps(fields, sort_keys=True)),
            )
        return f"{cursor.lastrowid}-0"

    def xlen(self, stream: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM stream_entries WHERE stream_name = ?", (stream,)
            ).fetchone()
        return int(row[0])

    def xrange(self, stream: str) -> list[tuple[str, dict[str, str]]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT entry_id, fields_json FROM stream_entries WHERE stream_name = ? ORDER BY entry_id",
                (stream,),
            ).fetchall()
        return [(f"{entry_id}-0", json.loads(fields_json)) for entry_id, fields_json in rows]

    def setnx(self, key: str, value: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO stream_idempotency (idempotency_key) VALUES (?)", (key,)
            )
        return cursor.rowcount == 1

    def eval(self, script: str, keys: list[str], args: list[str]) -> str | None:
        """Atomically claim an event key and append its serialized stream payload."""
        claim_key, stream = keys
        fields = dict(zip(args[::2], args[1::2], strict=True))
        return self.xadd_once(stream, claim_key, fields)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class RedisSocketStreamClient:
    """Dependency-free Redis Stream client for the Docker Compose Redis service."""

    def __init__(self, redis_url: str, timeout_seconds: float = 1.0) -> None:
        parsed = urlparse(redis_url)
        if parsed.scheme not in {"redis", "rediss"}:
            raise ValueError("Redis URL must use redis:// or rediss://")
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 6379
        self._password = unquote(parsed.password) if parsed.password else None
        self._database = int(parsed.path.lstrip("/") or "0")
        self._timeout_seconds = timeout_seconds
        self._use_tls = parsed.scheme == "rediss"

    def xadd(self, stream: str, fields: dict[str, str]) -> str:
        command = ["XADD", stream, "*"]
        for key, value in fields.items():
            command.extend((key, value))
        result = self._execute(*command)
        return _decode(result)

    def xadd_once(self, stream: str, claim_key: str, fields: dict[str, str]) -> str | None:
        """Retry a pending append while suppressing completed event publications."""
        args = [item for pair in fields.items() for item in pair]
        result = self.eval(_RECOVERABLE_XADD_ONCE, [claim_key, stream], args)
        return None if result is None else _decode(result)

    def xlen(self, stream: str) -> int:
        return int(self._execute("XLEN", stream))

    def xrange(self, stream: str) -> list[tuple[str, dict[str, str]]]:
        response = self._execute("XRANGE", stream, "-", "+")
        return [
            (_decode(entry_id), _fields_from_response(fields))
            for entry_id, fields in response
        ]

    def setnx(self, key: str, value: str) -> bool:
        return self._execute("SET", key, value, "NX") is not None

    def eval(self, script: str, keys: list[str], args: list[str]) -> object:
        return self._execute("EVAL", script, str(len(keys)), *keys, *args)

    def _execute(self, *command: str) -> object:
        connection = socket.create_connection((self._host, self._port), self._timeout_seconds)
        if self._use_tls:
            connection = ssl.create_default_context().wrap_socket(
                connection, server_hostname=self._host
            )
        with connection:
            stream = connection.makefile("rwb")
            if self._password is not None:
                _write_command(stream, "AUTH", self._password)
                _read_response(stream)
            if self._database:
                _write_command(stream, "SELECT", str(self._database))
                _read_response(stream)
            _write_command(stream, *command)
            return _read_response(stream)


def _write_command(stream, *parts: str) -> None:
    encoded = [part.encode() for part in parts]
    stream.write(f"*{len(encoded)}\r\n".encode())
    for value in encoded:
        stream.write(f"${len(value)}\r\n".encode())
        stream.write(value + b"\r\n")
    stream.flush()


def _read_response(stream) -> object:
    prefix = stream.read(1)
    if prefix == b"+":
        return stream.readline().rstrip(b"\r\n")
    if prefix == b"-":
        raise RuntimeError(_decode(stream.readline().rstrip(b"\r\n")))
    if prefix == b":":
        return int(stream.readline())
    if prefix == b"$":
        length = int(stream.readline())
        if length < 0:
            return None
        return stream.read(length + 2)[:-2]
    if prefix == b"*":
        length = int(stream.readline())
        if length < 0:
            return None
        return [_read_response(stream) for _ in range(length)]
    raise RuntimeError("Invalid Redis RESP response")


def _fields_from_response(response: Sequence[object]) -> dict[str, str]:
    return {
        _decode(response[index]): _decode(response[index + 1])
        for index in range(0, len(response), 2)
    }


def _decode(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
