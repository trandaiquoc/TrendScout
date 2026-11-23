"""Stage dashboard files as standalone Vercel static assets."""

from pathlib import Path

script = Path(__file__).resolve()
backend = script.parents[1]
target = backend / "public" / "dashboard"
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
    raise FileNotFoundError("Frontend/public/index.html is unavailable for the Vercel dashboard bundle")

assets = {
    path.relative_to(source).as_posix(): path
    for path in sorted(source.rglob("*"))
    if path.is_file() and not any(part.startswith(".") for part in path.relative_to(source).parts)
}
if "index.html" not in assets:
    raise FileNotFoundError("Frontend/public/index.html was not included in the Vercel dashboard bundle")

target.mkdir(parents=True, exist_ok=True)
expected = set(assets)
for existing in target.rglob("*"):
    if existing.is_file() and existing.relative_to(target).as_posix() not in expected:
        existing.unlink()
for relative, path in assets.items():
    destination = target / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(path.read_bytes())
print(f"Staged {len(assets)} dashboard assets in {target}")
