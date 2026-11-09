"""Bundle dashboard source into an importable module for the Vercel function."""
from pathlib import Path

backend = Path(__file__).resolve().parents[1]
source = backend.parent / "Frontend" / "public"
names = ("index.html", "app.js", "styles.css")
assets = {name: (source / name).read_bytes() for name in names}
(backend / "app" / "dashboard_assets.py").write_text(
    '"""Generated during the Vercel build; do not edit."""\nASSETS = ' + repr(assets) + "\n",
    encoding="utf-8",
)
