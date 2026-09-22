import subprocess, pathlib

def count_crlf(b):
    return sum(1 for i in range(len(b) - 1) if b[i] == 13 and b[i + 1] == 10)

for label, root, ref in [
    ("new", pathlib.Path(r"E:\vhs-upscale\worktrees\tape-qtgmc-long-processing"), "HEAD"),
    ("old", pathlib.Path(r"E:\vhs-upscale\worktrees\tape-rocm-rx7900xt-luna"), "HEAD"),
]:
    rel = "scripts/wsl/setup_rocm_env.sh"
    stage = subprocess.run(["git", "-C", str(root), "ls-files", "--stage", "--", rel], capture_output=True, text=True, check=True).stdout.strip()
    blob = subprocess.run(["git", "-C", str(root), "cat-file", "blob", ref + ":" + rel], capture_output=True, check=True).stdout
    disk_p = root / rel
    disk = disk_p.read_bytes()
    print(label, "stage:", stage)
    print(label, "blob len:", len(blob), "crlf:", count_crlf(blob))
    print(label, "disk len:", len(disk), "crlf:", count_crlf(disk), "equal:", disk == blob)

root = pathlib.Path(r"E:\vhs-upscale\worktrees\tape-qtgmc-long-processing")
files = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=True).stdout.splitlines()
mismatch = []
for rel in files:
    p = root / rel
    blob = subprocess.run(["git", "-C", str(root), "cat-file", "blob", "HEAD:" + rel], capture_output=True, check=True).stdout
    disk = p.read_bytes() if p.exists() else b"<missing>"
    if disk != blob:
        mismatch.append((rel, len(blob), len(disk)))
print("sweep: files:", len(files), "mismatch:", len(mismatch))
for rel, bl, dl in mismatch:
    print("  ", rel, "blob", bl, "disk", dl)
