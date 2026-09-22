import subprocess, os, stat
root = r"E:\vhs-upscale\worktrees\tape-qtgmc-long-processing"
git = r"C:\Program Files\Git\cmd\git.exe"
def run(args, **kw):
    return subprocess.run([git] + args, cwd=root, capture_output=True, text=True, **kw)
for rel in ["LICENSE", "main.py", "scripts/wsl/setup_rocm_env.sh"]:
    wt = run(["hash-object", "--", rel]).stdout.strip()
    head = run(["rev-parse", "HEAD:" + rel]).stdout.strip()
    p = os.path.join(root, rel)
    stt = os.stat(p)
    attr = "readonly" if stt.st_mode & stat.ST_READ_ONLY else "rw"
    print(rel, "wt==HEAD:", wt == head, "size:", stt.st_size, attr)
    if wt != head:
        print("  wt:", wt)
        print("  hd:", head)
raw = run(["diff", "--raw"]).stdout
print("raw diff lines:", len(raw.splitlines()))
print(raw[:300])

