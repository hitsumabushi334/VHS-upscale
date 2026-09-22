import sys, types, json, tempfile
from pathlib import Path
import real_world_inference as rwi
import numpy as np
from PIL import Image

root = Path("E:/vhs-upscale/work/tmp/t9directrun")
import shutil
shutil.rmtree(root, ignore_errors=True)
root.mkdir(parents=True)

def write_png(p, v):
    Image.fromarray(np.full((4,6,3), v, dtype=np.uint8), mode="RGB").save(p)

def make_case(name, problem, reference_count=2):
    base = root / name
    inr = base/"input_frames"; rfr = base/"reference_frames"
    inr.mkdir(parents=True); rfr.mkdir(parents=True)
    in_names = ["000000028.png","000000029.png","000000030.png"]
    for i,n in enumerate(in_names): write_png(inr/n, i+1)
    ref_names = ["000000001.png","000000002.png"]
    for i,n in enumerate(ref_names): write_png(rfr/n, i+10)
    mapping = {n: ref_names[:reference_count] for n in in_names}
    if problem == "missing_mapping_key": del mapping["000000029.png"]
    elif problem == "missing_reference": mapping["000000029.png"] = ["000000999.png","000000001.png"]
    elif problem == "short_mapping": mapping["000000029.png"] = ["000000001.png"]
    elif problem == "name_mismatch":
        del mapping["000000029.png"]; mapping["000000031.png"] = ["000000001.png","000000002.png"]
    mp = base/"references.json"; mp.write_text(json.dumps(mapping))
    return base, inr, mp, rfr

class _Cuda:
    @staticmethod
    def is_available(): return False
class _Device:
    type = "cpu"
fake = types.SimpleNamespace(cuda=_Cuda(), device=lambda _n: _Device())
sys.modules["torch"] = fake

results = {}
for problem in ["missing_mapping_key","missing_reference","short_mapping","name_mismatch"]:
    base, inr, mp, rfr = make_case(problem, problem)
    reached = []
    orig_restore = rwi.restore_frames
    rwi.extract_video_frames = lambda *a, **k: None
    rwi.select_references = lambda *a, **k: None
    rwi.restore_frames = lambda **k: reached.append(True)
    argv = ["--input-path","s.mkv","--output-path",str(base/"out"),
            "--input-frames-path",str(inr),"--reference-map-path",str(mp),
            "--reference-frames-path",str(rfr),"--skip-reference-selection",
            "--global-start-frame","28","--num-reference-frames","2","--preprocess-mode","none","--num-workers","0"]
    try:
        args = rwi.build_parser().parse_args(argv)
        rwi.real_world_test(args)
        results[problem] = ("NO-ERROR", reached)
    except Exception as e:
        results[problem] = (type(e).__name__, reached)
    finally:
        rwi.restore_frames = orig_restore

for k,v in results.items():
    print(k, "->", v[0], "model_reached:", v[1])
print("done")

