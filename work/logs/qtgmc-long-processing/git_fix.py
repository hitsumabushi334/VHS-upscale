import subprocess
root = r"E:\vhs-upscale\worktrees\tape-qtgmc-long-processing"
git = r"C:\Program Files\Git\cmd\git.exe"
def run(args):
    r = subprocess.run([git] + args, cwd=root, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr
rc, out, err = run(["update-index", "--really-refresh"])
print("really-refresh rc:", rc, "out:", out[:200], "err:", err[:200])
rc, out, err = run(["status", "--porcelain"])
lines = out.splitlines()
print("status lines after refresh:", len(lines))
if lines:
    print("\n".join(lines[:5]))

