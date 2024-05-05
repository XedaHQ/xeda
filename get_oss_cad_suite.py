#!/usr/bin/env python3

# pip install progressbar
import progressbar

import json
import pprint
import urllib.request
import platform
from pathlib import Path
from shutil import unpack_archive, rmtree

system = platform.system().lower()
arch = platform.machine().lower()

if arch == "x86_64" or arch == "amd64":
    arch = "x64"
elif arch == "aarch64":
    arch = "arm64"

# print("platform=", platform.platform())
print(system, arch)

repo = "YosysHQ/oss-cad-suite-build"

_json = json.loads(
    urllib.request.urlopen(
        urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github.v3+json"},
        )
    ).read()
)

assets = _json["assets"]

asset_ext = "tgz" if system in ["linux", "darwin"] else "zip"

assets = [
    asset
    for asset in assets
    if asset["name"].endswith(asset_ext) and f"{system}-{arch}" in asset["name"]
]

if not assets:
    print(f"No suitable asset found for {system}-{arch}.")
    exit(1)

asset = assets[0]
# pprint.pprint(asset)
name = asset["name"]
size = asset["size"]
updated_at = asset["updated_at"]
url = asset["browser_download_url"]


class MyProgressBar:
    def __init__(self):
        self.pbar = None

    def __call__(self, block_num, block_size, total_size):
        if not self.pbar:
            self.pbar = progressbar.ProgressBar(maxval=total_size)
            self.pbar.start()

        downloaded = block_num * block_size
        if downloaded < total_size:
            self.pbar.update(downloaded)
        else:
            self.pbar.finish()


archive_file = Path(name)
if archive_file.exists() and archive_file.stat().st_size == size:
    print(f"using previously downloaded {archive_file}...")
else:
    print(f"Downloading {name} ({size} Bytes) from {url}...")
    urllib.request.urlretrieve(url, name, MyProgressBar())


target_dir = Path.home() / ".xeda" / "tools"

# content of the archive
target_subdir = target_dir / "oss-cad-suite"

if target_dir.exists():
    if target_subdir.exists():
        print(f"Removing existing installation at {target_subdir}...")
        rmtree(target_subdir)
else:
    target_dir.mkdir(parents=True)

print(f"Unpacking {name} to {target_subdir}...")
unpack_archive(name, target_dir)

assert target_subdir.exists() and target_subdir.is_dir()
bin_dir = target_subdir / "bin"
assert bin_dir.exists() and bin_dir.is_dir()
print(f"Installation complete. Add {bin_dir} to your PATH.")
