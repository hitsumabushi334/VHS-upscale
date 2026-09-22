import subprocess, json
root = r"E:\vhs-upscale\worktrees\tape-qtgmc-long-processing"
git = r"C:\Program Files\Git\cmd\git.exe"
st = subprocess.run([git, "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True)
print("status lines:", len(st.stdout.splitlines()))
print(st.stdout[:800])
diff = subprocess.run([git, "diff", "--stat"], cwd=root, capture_output=True, text=True, check=True)
print("diff stat:", repr(diff.stdout[:400]))
diffh = subprocess.run([git, "diff", "--name-only"], cwd=root, capture_output=True, text=True, check=True)
print("diff name-only:", diffh.stdout[:400])
idx = subprocess.run([git, "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True, env=None)

