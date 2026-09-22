import subprocess, pathlib
root = pathlib.Path(r"E:\vhs-upscale\worktrees\tape-qtgmc-long-processing")
files = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=True).stdout.splitlines()
fixed = []
for rel in files:
    p = root / rel
    blob = subprocess.run(["git", "-C", str(root), "cat-file", "blob", "HEAD:" + rel], capture_output=True, check=True).stdout
    if not p.exists():
        fixed.append((rel, "missing"))
        continue
    disk = p.read_bytes()
    if disk != blob:
        p.write_bytes(blob)
        fixed.append((rel, "rewritten"))
print("files:", len(files), "fixed:", len(fixed))
for rel, why in fixed[:20]:
    print(why, rel)
