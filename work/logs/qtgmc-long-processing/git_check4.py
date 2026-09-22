import subprocess
root = r"E:\vhs-upscale\worktrees\tape-qtgmc-long-processing"
git = r"C:\Program Files\Git\cmd\git.exe"
def run(args, **kw):
    return subprocess.run([git] + args, cwd=root, capture_output=True, text=True, **kw)
for key in ["core.filemode", "core.autocrlf", "core.symlinks"]:
    r = run(["config", "--get", key])
    print(key, "=", repr(r.stdout.strip()) if r.returncode == 0 else "(unset)")
# Compare index mode vs worktree mode on one file
r = run(["ls-files", "-s", "--", "LICENSE"])
print("index:", r.stdout.strip())
import os
print("worktree mode oct:", oct(os.stat(os.path.join(root, "LICENSE")).st_mode & 0o777))

