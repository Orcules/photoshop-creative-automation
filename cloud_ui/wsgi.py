import os
import sys

# Ensure project root on sys.path (one level up from this file)
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

# Configure tasks dir before importing the Flask app
tasks_dir = os.path.join(root_dir, "Tasks")
os.environ.setdefault("TASKS_DIR", tasks_dir)

from cloud_ui.app import app as application


