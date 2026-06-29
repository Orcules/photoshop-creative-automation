from __future__ import annotations

import os
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# Only these scripts may be queued for execution by the local agent
ALLOWED_SCRIPTS = ['main.py']

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-only-change-me')

# Ensure Tasks directory exists
TASKS_DIR = Path(os.environ.get('TASKS_DIR', 'Tasks'))
TASKS_DIR.mkdir(parents=True, exist_ok=True)

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def write_task_json_atomic(filename: str, data: dict):
    """Write JSON file atomically to avoid corruption"""
    tmp_path = TASKS_DIR / f"{filename}.tmp"
    final_path = TASKS_DIR / filename
    
    # Write to temporary file first
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    # Atomic replace
    tmp_path.replace(final_path)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/tasks', methods=['GET'])
def list_tasks():
    """List all tasks"""
    tasks = []
    for file_path in TASKS_DIR.glob("*.json"):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                task_data = json.load(f)
                tasks.append(task_data)
        except Exception:
            continue
    
    # Sort by creation time (newest first)
    tasks.sort(key=lambda t: t.get('created_at', ''), reverse=True)
    return jsonify(tasks)

@app.route('/tasks', methods=['POST'])
def create_task():
    """Create a new task"""
    try:
        data = request.get_json()

        script = data.get('script', 'main.py')
        if script not in ALLOWED_SCRIPTS:
            return jsonify({"success": False, "error": "Invalid script"}), 400

        task_id = str(uuid.uuid4())
        now = now_iso()
        
        # Extract sheet name from kwargs if provided
        sheet_name = data.get('kwargs', {}).get('sheet_name', 'YOUR_SHEET_NAME')
        
        task_data = {
            "id": task_id,
            "title": data.get('title', 'Task'),
            "status": "pending",
            "priority": int(data.get('priority', 0)),
            "created_at": now,
            "updated_at": now,
            "script": script,
            "args": data.get('args', []),
            "kwargs": data.get('kwargs', {}),
            "progress": 0,
            "error": None
        }
        
        # Write task file
        filename = f"{task_id}.json"
        write_task_json_atomic(filename, task_data)
        
        flash(f'Task "{task_data["title"]}" created successfully!', 'success')
        return jsonify({"success": True, "task_id": task_id}), 201
        
    except Exception as e:
        flash(f'Error creating task: {str(e)}', 'error')
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/tasks/<task_id>/cancel', methods=['POST'])
def cancel_task(task_id):
    """Cancel a task (change status to stopped)"""
    try:
        filename = f"{task_id}.json"
        file_path = TASKS_DIR / filename
        
        if not file_path.exists():
            return jsonify({"success": False, "error": "Task not found"}), 404
        
        # Read current task data
        with open(file_path, 'r', encoding='utf-8') as f:
            task_data = json.load(f)
        
        # Update status to stopped
        task_data['status'] = 'stopped'
        task_data['updated_at'] = now_iso()
        
        # Write back atomically
        write_task_json_atomic(filename, task_data)
        
        flash(f'Task "{task_data["title"]}" cancelled successfully!', 'success')
        return jsonify({"success": True})
        
    except Exception as e:
        flash(f'Error cancelling task: {str(e)}', 'error')
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/tasks/<task_id>/delete', methods=['POST'])
def delete_task(task_id):
    """Delete a task file"""
    try:
        filename = f"{task_id}.json"
        file_path = TASKS_DIR / filename
        
        if not file_path.exists():
            return jsonify({"success": False, "error": "Task not found"}), 404
        
        # Delete the file
        file_path.unlink()
        
        flash('Task deleted successfully!', 'success')
        return jsonify({"success": True})
        
    except Exception as e:
        flash(f'Error deleting task: {str(e)}', 'error')
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    host = os.environ.get('HOST', '127.0.0.1')
    app.run(debug=debug, host=host, port=5000)


