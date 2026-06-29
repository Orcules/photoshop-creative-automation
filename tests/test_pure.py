"""Tests for the pure (non-Photoshop) parts of the codebase."""

import importlib
import json

import pytest

from color_automation import hex_to_rgb, is_valid_hex_color
from local_agent.task_store import LocalTaskStore


# ---------------------------------------------------------------------------
# color_automation
# ---------------------------------------------------------------------------

def test_hex_to_rgb_six_digit():
    assert hex_to_rgb("#FF0000") == (255, 0, 0)
    assert hex_to_rgb("#00ff00") == (0, 255, 0)
    assert hex_to_rgb("0000FF") == (0, 0, 255)


def test_hex_to_rgb_three_digit_shorthand():
    assert hex_to_rgb("#FFF") == (255, 255, 255)
    assert hex_to_rgb("#abc") == (170, 187, 204)


def test_hex_to_rgb_invalid_length_raises():
    with pytest.raises(ValueError):
        hex_to_rgb("#FFFF")


def test_is_valid_hex_color_accepts_valid():
    assert is_valid_hex_color("#A1B2C3") is True
    assert is_valid_hex_color("#fff") is True
    assert is_valid_hex_color("  #A1B2C3  ") is True


def test_is_valid_hex_color_rejects_invalid():
    assert is_valid_hex_color(None) is False
    assert is_valid_hex_color("") is False
    assert is_valid_hex_color("default") is False
    assert is_valid_hex_color("A1B2C3") is False  # missing '#'
    assert is_valid_hex_color("#GGGGGG") is False
    assert is_valid_hex_color("#12345") is False


# ---------------------------------------------------------------------------
# local_agent.task_store.LocalTaskStore
# ---------------------------------------------------------------------------

def test_local_task_store_round_trip(tmp_path):
    store = LocalTaskStore(str(tmp_path / "Tasks"))
    data = {"id": "abc-123", "status": "pending", "progress": 0}

    store.write_task_json_atomic("abc-123.json", data)

    assert list(store.list_task_files()) == ["abc-123.json"]
    assert store.read_task_json("abc-123.json") == data

    # No leftover tmp file after the atomic replace
    leftovers = list((tmp_path / "Tasks").glob("*.tmp"))
    assert leftovers == []


def test_local_task_store_overwrite(tmp_path):
    store = LocalTaskStore(str(tmp_path))
    store.write_task_json_atomic("t.json", {"id": "t", "status": "pending"})
    store.write_task_json_atomic("t.json", {"id": "t", "status": "done"})
    assert store.read_task_json("t.json")["status"] == "done"


# ---------------------------------------------------------------------------
# cloud_ui Flask app (routes that do not execute scripts)
# ---------------------------------------------------------------------------

@pytest.fixture()
def cloud_app(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKS_DIR", str(tmp_path / "Tasks"))
    import cloud_ui.app as app_module
    importlib.reload(app_module)  # pick up the TASKS_DIR override
    app_module.app.config["TESTING"] = True
    return app_module


def test_list_tasks_empty(cloud_app):
    client = cloud_app.app.test_client()
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_create_task_rejects_unlisted_script(cloud_app):
    client = cloud_app.app.test_client()
    resp = client.post(
        "/tasks",
        data=json.dumps({"title": "bad", "script": "evil.py"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_create_task_accepts_allowlisted_script(cloud_app):
    client = cloud_app.app.test_client()
    resp = client.post(
        "/tasks",
        data=json.dumps({"title": "ok", "script": "main.py"}),
        content_type="application/json",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["success"] is True
    task_file = cloud_app.TASKS_DIR / f"{body['task_id']}.json"
    assert task_file.exists()
