"""Build the exact wheel shipped by this independent npm launcher."""

import json
import shutil
import subprocess
import zipfile
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = json.loads((ROOT / "npm/package.json").read_text())
output = ROOT / "npm/vendor"
output.mkdir(exist_ok=True)
# uv writes a wildcard .gitignore into its output directory. npm must not inherit it.
(output / ".npmignore").write_text("# Include the versioned wheel in npm.\n")
distributions = ROOT / "dist"
distributions.mkdir(exist_ok=True)
for wheel in output.glob("*.whl"):
    wheel.unlink()
subprocess.run(
    ["uv", "build", "--out-dir", str(distributions)],
    cwd=ROOT / "ethercat-python",
    check=True,
)
release_name = package["name"].replace("-", "_") + "-" + package["version"]
shutil.copy2(distributions / (release_name + "-py3-none-any.whl"), output)
wheels = list(output.glob("*.whl"))
assert len(wheels) == 1, wheels
with zipfile.ZipFile(wheels[0]) as archive:
    metadata = BytesParser().parsebytes(
        archive.read(
            next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        )
    )
    assert metadata["Name"] == package["name"]
    assert metadata["Version"] == package["version"]
    assert "ethercat_mcp/_contract/tools.json" in archive.namelist()
