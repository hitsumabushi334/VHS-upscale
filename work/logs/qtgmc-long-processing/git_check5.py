import subprocess
root = r"E:\vhs-upscale\worktrees\tape-qtgmc-long-processing"
git = r"C:\Program Files\Git\cmd\git.exe"
def run(args, **kw):
    return subprocess.run([git] + args, cwd=root, capture_output=True, text=True, **kw)
r = run(["ls-files", "-v", "--", "LICENSE", "main.py"])
print("flags:", r.stdout.strip())
r = run(["config", "--get-regexp", "^filter\."])
print("filters:", repr(r.stdout.strip()) if r.returncode == 0 else "none")

