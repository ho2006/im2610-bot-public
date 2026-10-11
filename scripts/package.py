"""Bundle deployable sources only; secrets, database and virtualenv never enter the zip."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
names = ["app.py", "core.py", "engine.py", "test_bot.py", "test_robustness.py", "test_collector.py", "test_data_quality.py", "requirements.txt", "Dockerfile",
         "compose.yaml", "README.md", "LICENSE", "BACKTEST.md", "STRATEGY_RESEARCH.md", "STRATEGY_SOURCES.md",
         "test_strategy_research.py", "test_research_minutes.py", "test_entry_filter.py", ".gitignore", ".dockerignore", ".env.example"]
files = [root / name for name in names]
files += sorted((root / "web").glob("*")) + sorted((root / "scripts").glob("*.py"))
output = root / "artifacts" / "im2610-bot.zip"
output.parent.mkdir(exist_ok=True)
with ZipFile(output, "w", ZIP_DEFLATED) as bundle:
    for file in files:
        bundle.write(file, "im2610-bot/" + file.relative_to(root).as_posix())
with ZipFile(output) as bundle:
    assert bundle.testzip() is None
    assert all(Path(name).name != ".env" and ".sqlite" not in name for name in bundle.namelist())
    print(f"Created {output.name}: {len(bundle.namelist())} source files, {output.stat().st_size} bytes. No credentials or account data.")
