import subprocess
root = r"E:\vhs-upscale\worktrees\tape-qtgmc-long-processing"
git = r"C:\Program Files\Git\cmd\git.exe"
def run(args, **kw):
    return subprocess.run([git] + args, cwd=root, capture_output=True, text=True, **kw)
r = run(["read-tree", "HEAD"])
print("read-tree rc:", r.returncode, r.stderr[:200])
r = run(["status", "--porcelain"])
lines = r.stdout.splitlines()
print("status lines after read-tree:", len(lines))
for l in lines[:8]:
    print(l)

