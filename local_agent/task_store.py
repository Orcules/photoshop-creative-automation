from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, Iterable, Optional

logger = logging.getLogger(__name__)


class TaskStore:
    def list_task_files(self) -> Iterable[str]:
        raise NotImplementedError

    def read_task_json(self, task_file: str) -> Dict:
        raise NotImplementedError

    def write_task_json_atomic(self, task_file: str, data: Dict) -> None:
        raise NotImplementedError


class LocalTaskStore(TaskStore):
    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def list_task_files(self) -> Iterable[str]:
        logger.debug("list_task_files base_dir=%s", self.base_dir)
        for p in self.base_dir.glob("*.json"):
            yield p.name

    def read_task_json(self, task_file: str) -> Dict:
        path = self.base_dir / task_file
        logger.debug("read %s", path)
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            logger.debug("read-ok id=%s status=%s", data.get("id"), data.get("status"))
            return data

    def write_task_json_atomic(self, task_file: str, data: Dict) -> None:
        path = self.base_dir / task_file
        tmp = path.with_suffix(".json.tmp")
        logger.debug(
            "write %s (tmp %s) status=%s progress=%s",
            path, tmp, data.get("status"), data.get("progress"),
        )
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        logger.debug("write-ok %s", path)


class RemoteTaskStore(TaskStore):
    def __init__(
        self,
        hostname: str,
        username: str,
        *,
        password: Optional[str] = None,
        port: int = 22,
        pkey_path: Optional[str] = None,
        base_dir: str = "Tasks",
    ) -> None:
        self.hostname = hostname
        self.username = username
        self.password = password
        self.port = port
        self.pkey_path = pkey_path
        self.base_dir = base_dir
        self._client: Optional["paramiko.SSHClient"] = None
        self._sftp: Optional["paramiko.SFTPClient"] = None
        # Connect on first use
        self._connect()

    def _connect(self) -> None:
        """Establish SSH/SFTP connection with keep-alive enabled"""
        import paramiko

        logger.info("Connecting to %s...", self.hostname)

        # Close existing connection if any
        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass

        self._client = paramiko.SSHClient()
        # Trust known hosts; warn (rather than silently trust) on unknown ones.
        # For production, pin the server key in known_hosts and switch to RejectPolicy.
        self._client.load_system_host_keys()
        self._client.set_missing_host_key_policy(paramiko.WarningPolicy())

        # Connect with keep-alive to prevent timeout
        if self.pkey_path:
            try:
                key = paramiko.Ed25519Key.from_private_key_file(self.pkey_path)
            except paramiko.SSHException:
                key = paramiko.RSAKey.from_private_key_file(self.pkey_path)
            self._client.connect(
                self.hostname,
                port=self.port,
                username=self.username,
                pkey=key,
                timeout=30
            )
        else:
            self._client.connect(
                self.hostname,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=30
            )

        # Enable TCP keep-alive to prevent connection drops
        transport = self._client.get_transport()
        if transport:
            transport.set_keepalive(60)  # Send keep-alive packet every 60 seconds

        self._sftp = self._client.open_sftp()

        # Ensure directory exists
        try:
            self._sftp.listdir(self.base_dir)
        except IOError:
            # mkdir -p behavior for nested paths
            parts = self.base_dir.strip('/').split('/')
            current = ''
            for part in parts:
                current = f"{current}/{part}" if current else f"/{part}"
                try:
                    self._sftp.listdir(current)
                except IOError:
                    self._sftp.mkdir(current)

        logger.info("Connected to %s", self.hostname)

    def _ensure_connected(self) -> None:
        """Ensure connection is alive, reconnect if needed"""
        try:
            # Quick check if connection is alive
            if self._sftp and self._client:
                transport = self._client.get_transport()
                if transport and transport.is_active():
                    return  # Connection is good
        except Exception as e:
            logger.debug("Connection check failed: %s", e)

        # Connection is dead, reconnect
        logger.info("Connection lost, reconnecting...")
        self._connect()

    def list_task_files(self) -> Iterable[str]:
        self._ensure_connected()
        logger.debug("list_task_files base_dir=%s", self.base_dir)
        for entry in self._sftp.listdir_attr(self.base_dir):
            if entry.filename.endswith('.json'):
                yield entry.filename

    def read_task_json(self, task_file: str) -> Dict:
        self._ensure_connected()
        remote_path = f"{self.base_dir}/{task_file}"
        logger.debug("read %s", remote_path)
        with self._sftp.open(remote_path, 'r') as f:
            data = json.load(f)
            logger.debug("read-ok id=%s status=%s", data.get("id"), data.get("status"))
            return data

    def write_task_json_atomic(self, task_file: str, data: Dict) -> None:
        self._ensure_connected()
        remote_path = f"{self.base_dir}/{task_file}"
        tmp_path = f"{remote_path}.tmp"
        logger.debug(
            "write %s (tmp %s) status=%s progress=%s",
            remote_path, tmp_path, data.get("status"), data.get("progress"),
        )

        try:
            # Remove existing tmp file if it exists
            try:
                self._sftp.remove(tmp_path)
            except IOError:
                pass  # File doesn't exist, which is fine

            # Write new tmp file
            with self._sftp.open(tmp_path, 'w') as f:
                f.write(json.dumps(data, ensure_ascii=False, indent=2))
                try:
                    f.flush()
                except Exception:
                    pass

            # Try atomic replace first
            try:
                self._sftp.rename(tmp_path, remote_path)
            except IOError:
                # If rename fails, try to remove old file and put new one
                try:
                    self._sftp.remove(remote_path)
                except IOError:
                    pass
                self._sftp.rename(tmp_path, remote_path)

            # Small sync delay to ensure remote FS visibility
            try:
                self._sftp.stat(remote_path)
                logger.debug("write-ok %s", remote_path)
            except Exception:
                pass

        except Exception as e:
            # Clean up tmp file on error
            try:
                self._sftp.remove(tmp_path)
            except IOError:
                pass
            logger.error("write-error %s: %s", remote_path, e)
            raise e

    def close(self) -> None:
        try:
            self._sftp.close()
        finally:
            self._client.close()
