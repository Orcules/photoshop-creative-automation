from __future__ import annotations

import json
import os
import time
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from local_agent.task_store import LocalTaskStore, RemoteTaskStore, TaskStore

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Task:
    id: str
    title: str
    status: str
    priority: int
    created_at: str
    updated_at: str
    script: str
    args: list
    kwargs: dict
    progress: int
    error: Optional[str]

    @staticmethod
    def from_json(data: Dict) -> "Task":
        return Task(
            id=data["id"],
            title=data.get("title", "Task"),
            status=data.get("status", "pending"),
            priority=int(data.get("priority", 0)),
            created_at=data.get("created_at", now_iso()),
            updated_at=data.get("updated_at", now_iso()),
            script=data.get("script", "main.py"),
            args=data.get("args", []),
            kwargs=data.get("kwargs", {}),
            progress=int(data.get("progress", 0)),
            error=data.get("error"),
        )


class TaskAgent:
    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self.current_process: Optional[subprocess.Popen] = None
        self.current_task_id: Optional[str] = None
        self._last_idle_log: float = 0.0
        # Behavior flags
        self.exit_on_done: bool = os.environ.get("AGENT_EXIT_ON_DONE", "0") == "1"
        try:
            self.exit_on_idle_seconds: Optional[float] = float(os.environ.get("AGENT_EXIT_ON_IDLE_SECONDS", "0")) or None
        except Exception:
            self.exit_on_idle_seconds = None

    def read_task(self, task_id: str) -> Task:
        data = self.store.read_task_json(f"{task_id}.json")
        return Task.from_json(data)

    def write_task_atomic(self, task: Task) -> None:
        self.store.write_task_json_atomic(f"{task.id}.json", task.__dict__)

    def find_next_task(self) -> Optional[Task]:
        tasks = []
        print(f"Scanning for tasks in directory: {self.store.base_dir}")
        for name in self.store.list_task_files():
            print(f"Found task file: {name}")
            try:
                data = self.store.read_task_json(name)
                print(f"Task id={data.get('id')} status={data.get('status')}")
                if data.get("status") == "pending":
                    print(f"Adding pending task: {data.get('title', 'Unknown')}")
                    tasks.append(Task.from_json(data))
            except Exception as e:
                print(f"Error reading task {name}: {e}")
                continue
        print(f"Total pending tasks found: {len(tasks)}")
        tasks.sort(key=lambda t: (-t.priority, t.created_at))
        return tasks[0] if tasks else None

    def update_status(self, task: Task, status: str, *, error: Optional[str] = None, progress: Optional[int] = None) -> None:
        print(f"Updating task {task.id} -> status='{status}' progress={progress if progress is not None else task.progress} error={error}")
        task.status = status
        task.updated_at = now_iso()
        if error is not None:
            task.error = error
        if progress is not None:
            task.progress = progress
        # Retry write a few times in case of transient SFTP issues
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                self.write_task_atomic(task)
                print(f"Task {task.id} status persisted (attempt {attempt+1})")
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                print(f"Persist failed for {task.id} (attempt {attempt+1}): {exc}")
                time.sleep(0.5 * (attempt + 1))
        if last_exc is not None:
            print(f"ERROR: Could not persist task {task.id} after retries: {last_exc}")

    def was_cancelled(self, task: Task) -> bool:
        try:
            latest = self.read_task(task.id)
            return latest.status == "stopped"
        except FileNotFoundError:
            # If the file is temporarily unavailable (e.g., being written atomically),
            # do NOT treat it as cancelled. Assume it's still active.
            return False

    def run_task(self, task: Task) -> None:
        self.update_status(task, "processing", progress=0)
        self.current_task_id = task.id

        # Compose command; ensure we call Python with the project's working dir
        # Use the virtual environment Python if available
        python_exe = os.environ.get("PYTHON_EXECUTABLE", "python")
        if os.path.exists(".venv/Scripts/python.exe"):
            python_exe = ".venv/Scripts/python.exe"
        elif os.path.exists(".venv/bin/python"):
            python_exe = ".venv/bin/python"
        
        args = [python_exe, task.script, *map(str, task.args)]
        print(f"[agent] Launching task {task.id}: args={args} cwd={Path.cwd()}")
        
        # Set environment variables from task kwargs
        env = os.environ.copy()
        print(f"Task kwargs keys: {sorted(task.kwargs.keys())}")
        for key, value in task.kwargs.items():
            # Convert kwargs keys to environment variable names
            env_key = key.upper()
            env[env_key] = str(value)
            print(f"Setting environment variable: {env_key}")
        # Ensure SHEET_NAME is set even if the key came under different variants
        sheet_candidates = [
            "SHEET_NAME",
            "sheet_name",
            "sheet",
            "SHEET",
        ]
        if not any(k in env for k in ["SHEET_NAME"]):
            for cand in sheet_candidates:
                if cand in task.kwargs:
                    env["SHEET_NAME"] = str(task.kwargs[cand])
                    print(f"[agent] Normalized SHEET_NAME from kwarg '{cand}'")
                    break

        # Normalize sheet URL into GOOGLE_SHEET_URL expected by main.py
        url_candidates = [
            "GOOGLE_SHEET_URL",
            "sheet_url",
            "url",
        ]
        if not any(k in env for k in ["GOOGLE_SHEET_URL"]):
            for cand in url_candidates:
                if cand in task.kwargs:
                    env["GOOGLE_SHEET_URL"] = str(task.kwargs[cand])
                    print(f"[agent] Normalized GOOGLE_SHEET_URL from kwarg '{cand}'")
                    break

        interesting_env = []
        for k in [
            "SHEET_NAME",
            "GOOGLE_SHEET_URL",
            "GCS_BUCKET",
            "SERVICE_ACCOUNT_JSON",
        ]:
            if k in env:
                interesting_env.append(k)
        print(f"[agent] Environment vars set: {', '.join(interesting_env) if interesting_env else 'none of interest'}")

        try:
            creationflags = 0
            if os.name == "nt":
                # Enable CTRL_BREAK_EVENT delivery to child group on Windows
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

            self.current_process = subprocess.Popen(
                args,
                cwd=str(Path.cwd()),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',  # Replace invalid chars instead of crashing
                bufsize=1,
                creationflags=creationflags,
                env=env,
            )

            assert self.current_process.stdout is not None
            for line in self.current_process.stdout:
                print(f"[child] {line}", end="")
                # Heartbeat + basic progress heuristics
                if self.was_cancelled(task):
                    self.terminate_current()
                    self.update_status(task, "stopped")
                    return
                if "progress:" in line.lower():
                    try:
                        pct = int(''.join(ch for ch in line if ch.isdigit())[:3])
                        pct = max(0, min(100, pct))
                        self.update_status(task, "processing", progress=pct)
                    except Exception:
                        pass
            code = self.current_process.wait()
            print(f"[agent] Process for task {task.id} exited with code {code}")
            if self.was_cancelled(task):
                self.update_status(task, "stopped")
            elif code == 0:
                print(f"[agent] Marking task {task.id} as done")
                self.update_status(task, "done", progress=100)
            else:
                print(f"[agent] Marking task {task.id} as error")
                self.update_status(task, "error", error=f"exit code {code}")
        except Exception as exc:
            self.update_status(task, "error", error=str(exc))
        finally:
            self.current_process = None
            self.current_task_id = None

    def terminate_current(self) -> None:
        if self.current_process and self.current_process.poll() is None:
            try:
                if os.name == "nt":
                    try:
                        print(f"[agent] Sending CTRL_BREAK to PID {self.current_process.pid}")
                        self.current_process.send_signal(signal.CTRL_BREAK_EVENT)
                    except Exception:
                        print(f"[agent] Terminating PID {self.current_process.pid}")
                        self.current_process.terminate()
                else:
                    print(f"[agent] Terminating PID {self.current_process.pid}")
                    self.current_process.terminate()
                try:
                    self.current_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    print(f"[agent] Killing PID {self.current_process.pid} after timeout")
                    self.current_process.kill()
            except Exception:
                try:
                    self.current_process.kill()
                except Exception:
                    pass

    def loop(self, poll_interval_seconds: float = 2.0) -> None:
        print("Agent started. Waiting for tasks…")
        idle_since: Optional[float] = None
        while True:
            try:
                if self.current_process is None:
                    task = self.find_next_task()
                    if task:
                        print(f"Found task: {task.title} (ID: {task.id})")
                        # Re-check cancellation before starting
                        if task.status == "pending" and not self.was_cancelled(task):
                            print(f"Starting task: {task.title}")
                            self.run_task(task)
                            # Exit immediately after finishing a task if configured
                            if self.exit_on_done:
                                print("[agent] exit_on_done=1 → stopping agent after task completion")
                                return
                            # Reset idle timer after work
                            idle_since = None
                        else:
                            # Quietly skip tasks that are not ready
                            time.sleep(poll_interval_seconds)
                    else:
                        # Log an idle heartbeat infrequently to avoid console spam
                        now_ts = time.time()
                        if now_ts - self._last_idle_log > 30:
                            print("Idle: no pending tasks")
                            self._last_idle_log = now_ts
                        # Start or check idle timer if configured
                        if self.exit_on_idle_seconds is not None:
                            if idle_since is None:
                                idle_since = now_ts
                            elif now_ts - idle_since >= self.exit_on_idle_seconds:
                                print(f"[agent] exit_on_idle_seconds={self.exit_on_idle_seconds} reached → stopping agent")
                                return
                else:
                    # Still running; check cancellation signal
                    if self.current_task_id:
                        try:
                            t = self.read_task(self.current_task_id)
                            if t.status == "stopped":
                                self.terminate_current()
                        except FileNotFoundError:
                            self.terminate_current()
                time.sleep(poll_interval_seconds)
            except KeyboardInterrupt:
                print("Keyboard interrupt received, stopping agent")
                break
            except Exception as e:
                print(f"Error in main loop: {e}")
                time.sleep(poll_interval_seconds)


def main() -> None:
    # Choose store: remote via SSH/SFTP if REMOTE_HOST is set, otherwise local dir
    remote_host = os.environ.get("REMOTE_HOST")
    if remote_host:
        print(f"Agent mode: REMOTE host={remote_host}")
        store = RemoteTaskStore(
            hostname=remote_host,
            username=os.environ.get("REMOTE_USER", ""),
            password=os.environ.get("REMOTE_PASSWORD"),
            port=int(os.environ.get("REMOTE_PORT", "22")),
            pkey_path=os.environ.get("REMOTE_PKEY_PATH"),
            base_dir=os.environ.get("REMOTE_TASKS_DIR", "Tasks"),
        )
        try:
            base_dir = os.environ.get("REMOTE_TASKS_DIR", "Tasks")
            print(f"Remote tasks dir: {base_dir}")
        except Exception:
            pass
    else:
        tasks_dir = os.environ.get("TASKS_DIR", "Tasks")
        print(f"Agent mode: LOCAL dir={tasks_dir}")
        store = LocalTaskStore(tasks_dir)

    agent = TaskAgent(store=store)
    agent.loop()


if __name__ == "__main__":
    main()


