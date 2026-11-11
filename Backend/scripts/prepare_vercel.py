"""Bundle every dashboard static asset into an importable Vercel module."""

from pathlib import Path

backend = Path(__file__).resolve().parents[1]
source = backend.parent / "Frontend" / "public"
assets = {
    path.relative_to(source).as_posix(): path.read_bytes()
    for path in sorted(source.rglob("*"))
    if path.is_file() and not any(part.startswith(".") for part in path.relative_to(source).parts)
}
(backend / "app" / "dashboard_assets.py").write_text(
    '"""Generated during the Vercel build; do not edit."""\nASSETS = ' + repr(assets) + "\n",
    encoding="utf-8",
)
