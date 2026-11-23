"""Bundle every dashboard static asset into an importable Vercel module."""

from pathlib import Path

script = Path(__file__).resolve()
backend = script.parents[1]
target = backend / "app" / "dashboard_assets.py"
roots = [*script.parents, *Path.cwd().resolve().parents, Path.cwd().resolve()]
source = next(
    (
        root / "Frontend" / "public"
        for root in roots
        if (root / "Frontend" / "public" / "index.html").is_file()
    ),
    None,
)

if source is None:
    # Vercel's project root can omit sibling frontend files from the build
    # workspace even when they were included in the deployment upload. Keep a
    # prebundled copy when one is supplied with the source files; never emit an
    # empty ASSETS map that makes every dashboard route return 404.
    if target.is_file() and "ASSETS = " in target.read_text(encoding="utf-8"):
        raise SystemExit(0)
    raise FileNotFoundError("Frontend/public/index.html is unavailable for the Vercel dashboard bundle")

assets = {
    path.relative_to(source).as_posix(): path.read_bytes()
    for path in sorted(source.rglob("*"))
    if path.is_file() and not any(part.startswith(".") for part in path.relative_to(source).parts)
}
if not assets.get("index.html"):
    raise FileNotFoundError("Frontend/public/index.html was not included in the Vercel dashboard bundle")

target.write_text(
    '"""Generated during the Vercel build; do not edit."""\nASSETS = ' + repr(assets) + "\n",
    encoding="utf-8",
)
