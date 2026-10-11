"""The Runtime CodeZip only installs app/fieldwatch_agent/pyproject.toml, so app.py must import with just those
libraries. A missing one (shapely, Oct 10) means the Runtime never boots and every investigation silently fails."""
import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
PYPROJECT = AGENT / "cloud" / "agentcore" / "app" / "fieldwatch_agent" / "pyproject.toml"
# In agent/requirements.txt (local dev) but deliberately not packaged for the Runtime.
NOT_PACKAGED = ["rasterio", "pyogrio", "scipy", "PIL", "pystac_client", "skimage", "fastapi", "uvicorn", "ollama",
                "strands_tools", "pyproj"]


def _deps() -> set[str]:
    deps = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    return {d.split()[0].split("[")[0].split(">")[0].split("=")[0].split("~")[0].lower() for d in deps}


def test_shapely_is_packaged():
    assert "shapely" in _deps()


def test_app_imports_without_unpackaged_libraries():
    code = ("import sys\n"
            f"for m in {NOT_PACKAGED!r}: sys.modules[m] = None\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location('app', 'app.py')\n"
            "spec.loader.exec_module(importlib.util.module_from_spec(spec))\n")
    run = subprocess.run([sys.executable, "-c", code], cwd=AGENT, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr[-2000:]
