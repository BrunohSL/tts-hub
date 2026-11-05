"""Estado configurável pelo painel, em SQLite (volume `ttshub_data`).

- providers: ordem da cadeia (1º habilitado = principal), liga/desliga, timeout,
  cooldown do circuit breaker, voz e velocidade padrão.
- api_keys: um consumidor por chave (Guardia, o produto novo...), com prioridade
  na fila dos providers limitados. Só o hash fica no banco.
- audit: quem mudou o quê no painel.

Uma réplica, um worker: o acesso é serializado por um lock e o que é lido a cada
request (cadeia e chaves) fica em cache, invalidado a cada escrita.
"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

KEY_PREFIX = "tth_"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_key(raw: str) -> str:
    # Chave aleatória de 256 bits: sha256 basta (não é senha de humano).
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ConfigStore:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self._providers_cache: Optional[list] = None
        self._keys_cache: Optional[dict] = None

    def _migrate(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS providers (
                  name        TEXT PRIMARY KEY,
                  position    INTEGER NOT NULL,
                  enabled     INTEGER NOT NULL,
                  timeout_s   REAL    NOT NULL,
                  cooldown_s  INTEGER NOT NULL,
                  voice       TEXT,
                  speed       INTEGER NOT NULL DEFAULT 0,
                  updated_at  TEXT    NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_keys (
                  id            INTEGER PRIMARY KEY AUTOINCREMENT,
                  name          TEXT    NOT NULL,
                  prefix        TEXT    NOT NULL,
                  key_hash      TEXT    NOT NULL UNIQUE,
                  priority      INTEGER NOT NULL DEFAULT 0,
                  created_at    TEXT    NOT NULL,
                  created_by    TEXT,
                  last_used_at  TEXT,
                  revoked_at    TEXT
                );
                CREATE TABLE IF NOT EXISTS audit (
                  id      INTEGER PRIMARY KEY AUTOINCREMENT,
                  at      TEXT NOT NULL,
                  actor   TEXT NOT NULL,
                  action  TEXT NOT NULL,
                  detail  TEXT
                );
                """
            )

    # ------------------------------------------------------------------ providers

    def sync_providers(self, providers: dict) -> None:
        """Registra no banco provider novo do código (vai pro fim da cadeia)."""
        with self._lock, self._db:
            existing = {r["name"] for r in self._db.execute("SELECT name FROM providers")}
            last = self._db.execute("SELECT COALESCE(MAX(position), 0) FROM providers").fetchone()[0]
            for p in providers.values():
                if p.name in existing:
                    continue
                last += 1
                self._db.execute(
                    "INSERT INTO providers (name, position, enabled, timeout_s, cooldown_s, voice, speed, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                    (p.name, last, int(p.default_enabled), p.default_timeout_s, p.default_cooldown_s, p.default_speed, _now()),
                )
            self._providers_cache = None

    def providers(self) -> list:
        """Todos, na ordem da cadeia. Lista de dicts (cópia)."""
        with self._lock:
            if self._providers_cache is None:
                rows = self._db.execute("SELECT * FROM providers ORDER BY position, name").fetchall()
                self._providers_cache = [
                    {**dict(r), "enabled": bool(r["enabled"])} for r in rows
                ]
            return [dict(p) for p in self._providers_cache]

    def provider(self, name: str) -> Optional[dict]:
        return next((p for p in self.providers() if p["name"] == name), None)

    def update_provider(self, name: str, fields: dict, actor: str) -> None:
        allowed = {"enabled", "timeout_s", "cooldown_s", "voice", "speed"}
        changes = {k: v for k, v in fields.items() if k in allowed}
        if not changes:
            return
        if "enabled" in changes:
            changes["enabled"] = int(bool(changes["enabled"]))
        sets = ", ".join(f"{k} = ?" for k in changes)
        with self._lock, self._db:
            self._db.execute(
                f"UPDATE providers SET {sets}, updated_at = ? WHERE name = ?",
                (*changes.values(), _now(), name),
            )
            self._audit(actor, "provider.update", {"provider": name, **fields})
            self._providers_cache = None

    def reorder(self, names: list, actor: str) -> None:
        with self._lock, self._db:
            current = [r["name"] for r in self._db.execute("SELECT name FROM providers ORDER BY position, name")]
            if sorted(current) != sorted(names):
                raise ValueError("A nova ordem precisa conter exatamente os providers cadastrados")
            for i, name in enumerate(names, start=1):
                self._db.execute("UPDATE providers SET position = ?, updated_at = ? WHERE name = ?", (i, _now(), name))
            self._audit(actor, "provider.reorder", {"from": current, "to": names})
            self._providers_cache = None

    # ------------------------------------------------------------------- api keys

    def create_key(self, name: str, priority: int, actor: str) -> dict:
        raw = KEY_PREFIX + secrets.token_urlsafe(32)
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO api_keys (name, prefix, key_hash, priority, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                (name, raw[: len(KEY_PREFIX) + 6], hash_key(raw), priority, _now(), actor),
            )
            self._audit(actor, "key.create", {"id": cur.lastrowid, "name": name, "priority": priority})
            self._keys_cache = None
        # A chave em claro só existe nesta resposta.
        return {"id": cur.lastrowid, "name": name, "priority": priority, "key": raw}

    def update_key(self, key_id: int, priority: int, actor: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE api_keys SET priority = ? WHERE id = ? AND revoked_at IS NULL", (priority, key_id))
            self._audit(actor, "key.update", {"id": key_id, "priority": priority})
            self._keys_cache = None

    def revoke_key(self, key_id: int, actor: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (_now(), key_id))
            self._audit(actor, "key.revoke", {"id": key_id})
            self._keys_cache = None

    def keys(self) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, name, prefix, priority, created_at, created_by, last_used_at, revoked_at"
                " FROM api_keys ORDER BY revoked_at IS NOT NULL, name"
            ).fetchall()
            return [dict(r) for r in rows]

    def consumer_for(self, raw: str) -> Optional[dict]:
        """Consumidor dono da chave ativa, ou None."""
        with self._lock:
            if self._keys_cache is None:
                rows = self._db.execute(
                    "SELECT id, name, key_hash, priority FROM api_keys WHERE revoked_at IS NULL"
                ).fetchall()
                self._keys_cache = {r["key_hash"]: {"id": r["id"], "name": r["name"], "priority": r["priority"]} for r in rows}
            consumer = self._keys_cache.get(hash_key(raw))
            return dict(consumer) if consumer else None

    def touch_key(self, key_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (_now(), key_id))

    # ---------------------------------------------------------------------- audit

    def _audit(self, actor: str, action: str, detail: dict) -> None:
        # Chamado já dentro do lock/transação.
        self._db.execute(
            "INSERT INTO audit (at, actor, action, detail) VALUES (?, ?, ?, ?)",
            (_now(), actor, action, json.dumps(detail, ensure_ascii=False)),
        )

    def audit(self, limit: int = 50) -> list:
        with self._lock:
            rows = self._db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]
