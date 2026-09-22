import json
import sys

report = {"python": sys.version.split()[0]}
clip = None

try:
    import vapoursynth as vs
    report["vapoursynth"] = list(vs.__version__)
    core = vs.core
    try:
        clip = core.std.BlankClip(width=32, height=32, color=[0, 0, 0], length=4, fpsnum=30000, fpsden=1001, format=vs.YUV420P8, chromasite=vs.CHROMA_LEFT)
    except Exception as e1:
        report["blankclip_minimal_error"] = str(e1)
        clip = None
    if clip is not None:
        f0 = clip.get_frame(0)
        report["synthetic_first_frame"] = "ok"
        props = {}
        for key in ("_ColorSpace", "_ColorMatrix", "_ColorPrimaries", "_TransferCharacteristics", "_SampleAspectRatio", "_FieldBased", "_Width", "_Height"):
            if key in f0.props:
                props[key] = f0.props[key]
        report["frame_props"] = props
except Exception as e:
    report["error"] = str(e)

if clip is not None:
    try:
        from vsdeinterlace import QTempGaussMC
        q = QTempGaussMC()
        out = q.deinterlace(clip, tff=True)
        fo = out.get_frame(0)
        report["qtgmc_first_frame"] = "ok"
    except Exception as e:
        report["qtgmc_first_frame_error"] = str(e)

print(json.dumps(report, ensure_ascii=False))
