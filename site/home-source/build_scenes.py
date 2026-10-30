"""Build the original local workflow and riding illustrations."""
from pathlib import Path
import shutil
ROOT = Path(__file__).resolve().parent

def build_task_scene():
    target = ROOT / 'public/animations'
    target.mkdir(exist_ok=True)
    for name in ['task-workspace.html', 'task-workspace.css', 'task-workspace.js', 'pelican-ride.html']:
        shutil.copyfile(ROOT / 'scenes' / name, target / name)

if __name__ == '__main__':
    build_task_scene()
