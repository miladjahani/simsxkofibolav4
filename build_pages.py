"""Build a static GitHub Pages site from the FastAPI app.

GitHub Pages serves static files only, so this script:
  1. Copies the single-file UI from app/templates/index.html into docs/index.html,
     rewriting absolute /api/... URLs to relative paths that resolve as static files.
  2. Emits the read-only GET endpoints (workbook metadata, process schema/presets,
     health) as static JSON under docs/api/, mirroring the route paths.
  3. Copies the browser engine port (app/static_assets/engine.js) to docs/static/, so
     docs/index.html can run the SX-EW simulation and NSGA-II optimization entirely
     client-side (static mode is activated by the /api/health mode marker).

Usage: python build_pages.py
"""

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
API = DOCS / "api"

# Directories cleaned/recreated on every build.
BUILD_DIRS = [DOCS, API]


def clean():
    for d in BUILD_DIRS:
        if d.exists():
            shutil.rmtree(d)
    DOCS.mkdir(parents=True)


def copy_static_assets():
    """Copy committed static data, the browser engine and .nojekyll into docs/."""
    shutil.copy2(ROOT / ".nojekyll", DOCS / ".nojekyll")
    static = DOCS / "static"
    static.mkdir()
    shutil.copy2(ROOT / "app" / "static_assets" / "engine.js", static / "engine.js")
    data = ROOT / "app" / "data"
    (DOCS / "data").mkdir()
    for name in ("model_manifest.json", "workbook_meta.json"):
        shutil.copy2(data / name, DOCS / "data" / name)


def build_index():
    html = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf8")
    # Absolute API paths -> relative paths so the UI works from a project subpath
    # like https://user.github.io/repo/ without a <base> tag.
    replacements = {
        '"/api/': '"api/',
        "'/api/": "'api/",
        '`/api/': "`api/",
        '"/api/health"': '"api/health"',
    }
    for old, new in replacements.items():
        html = html.replace(old, new)
    (DOCS / "index.html").write_text(html, encoding="utf8")


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf8")


def build_api_snapshot():
    """Emit static JSON for the read-only GET endpoints.

    The template itself never imports the FastAPI app here: the build must work
    with only the standard library so the Actions build job needs no pip install.
    (The endpoint payloads below mirror app/main.py responses.)
    """
    meta = json.loads((ROOT / "app" / "data" / "workbook_meta.json").read_text(encoding="utf8"))
    write_json(API / "workbook", meta)
    write_json(API / "process" / "schema", {
        "species": ["Cu", "Fe", "H2SO4"],
        "units": {"flow": "user-defined mass/time", "concentration": "mass fraction"},
        "blocks": {
            "Ex": {"ports": ["raffinate", "organic"],
                   "parameters": ["D_Cu", "O_A", "Qmax_Cu", "isotherm_K", "isotherm_n",
                                  "acid_exponent", "phase_recovery",
                                  "phase_disengagement_time", "fe_extraction"]},
            "Px": {"ports": ["electrolyte", "lean_organic"],
                   "parameters": ["D_Cu", "A_O", "phase_recovery",
                                  "phase_disengagement_time", "fe_stripping", "acid_transfer"]},
            "Mixer": {"ports": ["out"], "parameters": []},
            "W": {"ports": ["out"], "parameters": ["fe_rejection"]},
            "EW": {"ports": ["product", "out"], "parameters": ["cu_deposition"]},
        },
        "solver": {"max_iter": "integer", "tolerance": "absolute component residual",
                   "relaxation": "0.05..0.99"},
    })
    write_json(API / "process" / "presets", {"presets": ["single_stage", "three_stage_countercurrent"]})
    write_json(API / "topologies", [])
    write_json(API / "health", {"ok": True, "mode": "static-client-side", "excel_model": "model.xlsx"})


def main():
    clean()
    copy_static_assets()
    build_index()
    build_api_snapshot()
    files = sorted(str(p.relative_to(DOCS)) for p in DOCS.rglob("*") if p.is_file())
    print(f"Built {len(files)} files into docs/:")
    for f in files:
        print(f"  {f}")


if __name__ == "__main__":
    main()
