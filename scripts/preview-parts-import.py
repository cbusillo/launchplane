from pathlib import Path
import runpy

if __name__ == "__main__":
    raise SystemExit(runpy.run_path(str(Path(__file__).with_name("preview-fixture.py")))["main"]())
