import io
CRLF = chr(13) + chr(10)
doc = r"E:\vhs-upscale\worktrees\tape-qtgmc-long-processing\docs\qtgmc-long-processing.md"
rec = r"E:\vhs-upscale\work\logs\qtgmc-long-processing\task9_record.md"
with io.open(doc, "r", encoding="utf-8", newline="") as f:
    content = f.read()
with io.open(rec, "r", encoding="utf-8", newline="") as f:
    record = f.read()
record = record.replace(chr(13), "").replace(chr(10), CRLF)
assert CRLF in content, "expected CRLF doc"
marker = "### Task 9 implementation record"
assert marker not in content, "task9 record already present"
if not content.endswith(CRLF):
    content += CRLF
with io.open(doc, "w", encoding="utf-8", newline="") as f:
    f.write(content + record)
print("OK")
