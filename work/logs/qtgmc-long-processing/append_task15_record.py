import io
doc = r"E:\vhs-upscale\worktrees\tape-qtgmc-long-processing\docs\qtgmc-long-processing.md"
rec = r"E:\vhs-upscale\work\logs\qtgmc-long-processing\task15_record_draft.md"
with io.open(doc, "r", encoding="utf-8", newline="") as f:
    content = f.read()
with io.open(rec, "r", encoding="utf-8", newline="") as f:
    record = f.read()
record = record.replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))
assert chr(13) + chr(10) not in content, "expected LF doc"
marker = "### Task 15 implementation record"
assert marker not in content, "task15 record already present"
if not content.endswith(chr(10)):
    content += chr(10)
with io.open(doc, "w", encoding="utf-8", newline="") as f:
    f.write(content + record)
print("OK")
