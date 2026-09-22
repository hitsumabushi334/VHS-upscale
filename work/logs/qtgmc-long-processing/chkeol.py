import subprocess
raw = subprocess.run(["git", "show", "HEAD:docs/qtgmc-long-processing.md"], cwd=r"E:\vhs-upscale\worktrees\tape-qtgmc-long-processing", capture_output=True).stdout
crlf = raw.count(b"\r\n")
lf = raw.count(b"\n") - crlf
print("head bytes=%d crlf=%d lf_only=%d" % (len(raw), crlf, lf))

