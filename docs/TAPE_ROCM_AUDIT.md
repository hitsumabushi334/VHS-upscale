# TAPE ROCm portability audit

Audit date: 2026-09-14  
Scope: Phase 1 only. The upstream repository was cloned for inspection; no upstream source, checkpoint, dependency, input video, or inference output was modified or downloaded.

## Executive finding

The TAPE model code is mostly vendor-neutral PyTorch. The only unambiguously NVIDIA/CUDA-only item in the repository is the original environment command in `install_requirements.sh:4`, which installs `pytorch-cuda=11.8` from the `nvidia` channel. The model source contains no custom CUDA/HIP extension, `.cu`/`.cpp` kernel, cuDNN/apex/xformers/CuPy dependency, `nvcc` invocation, or hard-coded `cuda:0` device.

The existing `torch.cuda` references are expected PyTorch API names on ROCm. ROCm PyTorch intentionally exposes the CUDA-compatible Python namespace, so they must not be mechanically changed to `torch.hip` or `torch.device("hip")`. The vendor port is small, but a runnable/reproducible inference setup is medium effort and actual GPU success remains unproven until the staged tests pass. Independent upstream logic defects also need correction before inference can be considered reliable; those defects are not ROCm-port requirements.

## Repository identity and assets

- Upstream: `https://github.com/miccunifi/TAPE`
- Default branch: `main`
- Audited commit: `fb97c608a1733dec5dbcd5aeb77ed84e29b793f7`
- Weights tag: `weights` -> `52f7871cc35fc6590639b0fc98835109ba6920b8`
- Working tree after clone: clean; 26 tracked files. `.git` is present: the earlier file listings intentionally omitted hidden Git metadata, while `git status`, `git rev-parse`, and `git show-ref` all succeeded.
- License: Creative Commons Attribution-NonCommercial 4.0 International, in `LICENSE` and documented at the end of `README.md`.
- Repository assets are only poster/thumbnail/teaser media. There is no `experiments/`, checkpoint, dataset, `requirements.txt`, `environment.yml`, Dockerfile, lockfile, or test fixture video.

The README identifies the following external assets:

1. Pretrained TAPE model: GitHub release `https://github.com/miccunifi/TAPE/releases/tag/weights`; place `checkpoint.pth` at `experiments/pretrained_model/checkpoint.pth` (`README.md:111-112,122,155-156`). The release says the checkpoint is trained on the proposed synthetic dataset.
2. Training/testing dataset: Google Drive link in `README.md:39,84`; it contains MP4 and LMDB train/test data.
3. Optional VMAF: external `ffmpeg` and a `vmaf` Python folder under `utils` (`README.md:77-79`).
4. Real-world inference also downloads OpenAI CLIP `RN50x4` unless its cache is populated (`real_world_inference.py:60`). The pinned CLIP package brings transitive `ftfy` and `regex` dependencies; neither is separately pinned by TAPE. The pinned CLIP source lists the model URL and SHA-256 in `clip.py:27-30,39-64,108-117` at commit `a1d0717`.
5. Constructing `SwinUNet` requests torchvision ImageNet Swin-T weights (`models/swin_feature_extractor.py:29`), so the standard `swin_t-704cedc3.pth` asset must already be cached or network access must be available even though the TAPE checkpoint is loaded afterward.
6. Training/evaluation use external ImageNet VGG-19 weights (`models/vgg_feature_extractor.py:43`) and pretrained LPIPS/AlexNet weights (`test.py:47`, `models/pl_model_module.py:43`); these are not bundled. They are not required by the real-world inference entry point, but they block reproducible training/evaluation until cached.

The audit did not download any of these assets. The TAPE checkpoint, CLIP `RN50x4`, and torchvision Swin-T weights are therefore current blockers for actual inference; the repository itself is not a runnable inference bundle. `ffmpeg` is only an upstream VMAF requirement, not a core real-world inference prerequisite (`README.md:77-79`, `utils/metrics.py:149-150`).

### Verification status

- **Verified from the local checkout:** repository identity, `main` ref, audited commit, `weights` tag ref, clean Git status, manifests, source line inventory, negative CUDA-extension scan, checkpoint-loading code paths, and absence of checkpoint/dataset/inference assets.
- **Officially documented but locally unverified:** the AMD ROCm 7.2 + PyTorch 2.9.1 compatibility tuple for RX 7900 XT under WSL2; the exact current compatibility of the user’s AMD Software 26.6.4 driver; and runtime compatibility of CLIP JIT, FP16 AMP, torchvision Swin-T, and Lightning on this machine.
- **Not tested in Phase 1:** Stage 1-3 GPU visibility/load/forward commands, checkpoint loading, any dependency installation, and any video inference. The “small” size estimate and unchanged-checkpoint conclusion are source-based assessments, not execution results.

## 1. Actual dependency composition

### Manifests/environment files found

Only these setup sources exist:

- `README.md:58-75`: recommends Anaconda, creates conda environment `TAPE` with Python `3.10`, then runs `install_requirements.sh`.
- `install_requirements.sh:4-5`: the sole dependency manifest/install script.

No conda environment export, Python requirements lock, `pyproject.toml`, `setup.py`, Dockerfile, or platform-specific ROCm manifest is present. Re-running the script today is not reproducible for transitive dependencies.

### Directly pinned packages

Conda, `install_requirements.sh:4`:

```text
pytorch==2.1.1
torchvision==0.16.1
pytorch-cuda=11.8       # NVIDIA/CUDA-only package selection
channels: pytorch, nvidia
```

Pip, `install_requirements.sh:5`:

```text
pandas==2.1.3
matplotlib==3.8.2       # listed twice
pyyaml==6.0.1
dotmap==1.3.30
tqdm==4.66.1
comet-ml==3.35.3
git+https://github.com/openai/clip.git@a1d0717
scikit-image==0.22.0
opencv-python==4.8.1.78
einops==0.7.0
lmdb==1.4.1
pytorch-lightning==2.1.2
lpips==0.1.4
```

The code also imports `numpy`, `PIL`/Pillow, and `torchmetrics` (`data/*.py`, `models/pl_model_module.py`, `real_world_inference.py`). They are not directly pinned in the script. `torchmetrics` is a particularly important implicit dependency because it is imported directly by `models/pl_model_module.py:4-7`; the installed version will be selected transitively by the resolver. `pyyaml` is installed but no YAML import was found in the repository.

Runtime roles are separated as follows:

- Real-world inference: PyTorch, torchvision, OpenAI CLIP plus its transitive `ftfy`/`regex`, NumPy, Pillow, OpenCV, scikit-image, tqdm, einops, and `pytorch_lightning` imported at inference time indirectly through `real_world_inference.py -> data.real_world_video_dataset -> utils.utils` (`utils/utils.py:6`).
- Training/evaluation: PyTorch Lightning, torchmetrics, LPIPS, pandas, matplotlib, Comet ML, LMDB, plus the real-world dependencies.
- Training/evaluation external weights: ImageNet VGG-19 and LPIPS/AlexNet.
- Optional metric path only: ffmpeg and external VMAF Python code.

## 2. Exhaustive CUDA/NVIDIA-specific inventory

The audit searched all tracked source, shell, and documentation files for CUDA, HIP, NVIDIA, cuDNN, apex, xformers, CuPy, nvcc, extension/compiler, Triton, NCCL, and related device strings.

### True CUDA/NVIDIA dependency

| File:line | Finding | ROCm disposition |
|---|---|---|
| `install_requirements.sh:4` | `pytorch-cuda=11.8` and `-c nvidia` | Must not be used for AMD. Replace with a ROCm PyTorch/torchvision pair from an AMD/PyTorch-supported wheel source. |

### `torch.cuda` references; normal ROCm API, not CUDA-only code

| File:line(s) | Code/use | Assessment |
|---|---|---|
| `utils/utils.py:8` | `torch.cuda.is_available()` and `torch.device("cuda")` | Normal ROCm PyTorch device selection. |
| `real_world_inference.py:20-23` | Same conditional device selection | Normal ROCm API; no `cuda:0` hard-code. |
| `real_world_inference.py:64,77,166` | `torch.cuda.amp.autocast()` | ROCm-facing CUDA namespace; validate FP16 kernels on RX 7900 XT. |
| `real_world_inference.py:111,196` | `torch.cuda.empty_cache()` | Cache-management API in the ROCm-compatible namespace; not a CUDA library dependency. |
| `test.py:71` | `torch.cuda.amp.autocast()` | Same as above, evaluation path. |
| `data/synthetic_video_dataset.py:141,158` | `torch.cuda.amp.autocast()` | Same as above, training/reference-generation path. |
| `main.py:111` | Lightning `accelerator="gpu"` gated by `torch.cuda.is_available()` | Generic Lightning GPU selection; training-only. |

Related but not CUDA-only:

- `real_world_inference.py:60` and `data/synthetic_video_dataset.py:138` pass the selected `torch.device` to `clip.load(..., jit=True)`.
- `pin_memory=True` and `non_blocking=True` occur in `real_world_inference.py:119-120` and `test.py:45,67-68`; these are generic data-transfer options.
- `utils/utils_models.py:61-66,146-150` creates attention masks on a caller-supplied `torch.device`, not on a fixed vendor device.
- `main.py:109-112` uses FP16 precision, generic `gpu` accelerator, and device index `[0]`; these require runtime validation for Lightning training but are not NVIDIA-only.

### Negative findings

No tracked file contains a custom CUDA/HIP extension, `.cu` or `.cpp` source, `torch.utils.cpp_extension`, `nvcc`, `torch.backends.cudnn`, apex, xformers, CuPy, NCCL, or an NVIDIA import. The model implementation is standard `torch.nn`, tensor indexing, convolution, attention, matrix multiplication, padding, and PixelShuffle (`models/swin_unet.py`, `models/swin_transformer_3d.py`, `models/mrsff.py`).

## 3. RX 7900 XT + ROCm risk points

1. **Installer mismatch — high certainty.** The pinned CUDA package/channel cannot be used on AMD. This is the primary mandatory change.
2. **Framework pairing — high.** TAPE pins the 2023 CUDA pair PyTorch 2.1.1/torchvision 0.16.1. ROCm wheels must match each other; do not install CUDA conda packages and then add ROCm libraries. The older Lightning/torchmetrics dependency set also has no lockfile and needs import validation.
3. **AMP — medium.** TAPE wraps CLIP and TAPE inference in `torch.cuda.amp.autocast()`. The API spelling is normal on ROCm, but the actual FP16 operator coverage/performance on the selected ROCm build must be proven by Stage 3. If it fails, first rerun in FP32/disable autocast; that is a precision fallback, not a kernel port.
4. **OpenAI CLIP JIT — medium/high.** The pinned CLIP implementation loads a TorchScript archive with `torch.jit.load(..., map_location=device)` and patches embedded device constants beginning with `cuda`. That behavior is designed for PyTorch’s CUDA-compatible device namespace and may work on ROCm, but `jit=True` plus FP16 is the highest external code-level risk in the real-world path. A failure here should be isolated from the TAPE model by testing CLIP separately.
5. **Torchvision Swin-T — medium.** `SwinFeatureExtractor` uses standard torchvision Swin-T weights and operators. The model constructor also performs the weight lookup/download before TAPE checkpoint loading. Validate the exact torchvision wheel and cache the weight before an offline run.
6. **Lightning training — medium, training-only.** `main.py:107-114` requests `precision=16`, a GPU accelerator, and device IDs. Real-world inference bypasses Lightning. If training is later required, test Lightning 2.1.2, DDPStrategy, FP16, and `devices=[0]` independently.
7. **Memory — unknown until Stage 2/3.** Default real-world inference uses five input and five reference frames, batch size one, and a maximum patch size of 512 (`real_world_inference.py:212-229`). The RX 7900 XT’s 20 GB VRAM may be sufficient, but no claim is made without an actual load/inference measurement.
8. **Upstream reference-selection defects — high for reliability, not ROCm-specific.** In `real_world_inference.py:95-100`, the code ranks within `img_features[indexes]` but indexes `img_names` with the resulting local positions without remapping through `indexes`; an empty candidate set makes `similar_imgs[0]` fail. The same candidate-remapping/empty-candidate defect exists in `data/synthetic_video_dataset.py:177-183`. These are upstream logic defects that block reliable inference independently of GPU backend.
9. **Upstream video-pipeline defects/caveats — high for reliability, not ROCm-specific.** `real_world_inference.py:127` compares `frame_height > frame_height`, always false, so portrait resize selection is wrong. `:43` has no `VideoCapture.isOpened()` check; `:139-145` has no writer-open checks; `:41-56` extracts the entire video to quality-100 JPEGs before inference; `:139-143` writes `mp4v` video without audio; and `:33` uses `exist_ok=False`, so an existing output directory aborts the run. Intermediate JPEGs/restored frames and videos can consume substantial disk space, and cleanup occurs only after successful completion.

The correct ROCm interpretation is therefore: `torch.cuda.is_available()` should be true, `torch.version.hip` should be non-`None`, and the device string should remain `"cuda"` under ROCm PyTorch. This is also documented by PyTorch’s ROCm verification guidance.

## 4. Exact files needing changes

Line references are against audited commit `fb97c60`.

### Mandatory for a ROCm environment

- `install_requirements.sh:4`: replace the NVIDIA conda install with a matched ROCm PyTorch/torchvision installation. The current command is not an AMD-compatible fallback.

### Recommended documentation/configuration changes, not required to prove vendor portability

- `README.md:58-75`: add a separate WSL2/ROCm installation path and explicitly warn users not to run the CUDA-only line from `install_requirements.sh` on AMD.
- `install_requirements.sh:5`: split or annotate the non-Torch packages, remove the duplicate matplotlib entry, and pin currently implicit runtime dependencies (`numpy`, Pillow, torchmetrics) after resolving the selected ROCm stack. This is reproducibility work, not a CUDA-kernel port.

### Upstream logic/video defects that block reliable inference, but are not ROCm requirements

- `real_world_inference.py:95-100`: remap local candidate positions through `indexes` before indexing `img_names`, and handle an empty candidate set before `similar_imgs[0]`.
- `data/synthetic_video_dataset.py:177-183`: apply the same reference-candidate remapping and empty-candidate handling.
- `real_world_inference.py:127`: compare `frame_height` with `frame_width` for portrait/landscape selection.
- `real_world_inference.py:33,43,139-145`: make output-directory behavior explicit, check `VideoCapture.isOpened()` and writer state, and avoid failing solely because an output directory already exists.
- `real_world_inference.py:41-56,188-203`: bound or stream JPEG extraction, plan disk cleanup on failure, and preserve audio through a separate muxing step if audio is required. The current OpenCV `mp4v` writer does not preserve audio.

### Conditional source changes after validation

- `real_world_inference.py:64,77,166`, `test.py:71`, `data/synthetic_video_dataset.py:141,158`: optionally migrate the deprecated-style AMP spelling to a small helper around `torch.amp.autocast("cuda", ...)`, with CPU disablement. Do this only if the selected PyTorch version warns or fails; `torch.cuda.amp` itself is not evidence of CUDA-only code.
- `real_world_inference.py:20-23,111,196` and `utils/utils.py:8`: optionally centralize device selection/cleanup. No ROCm-specific rewrite is justified by source inspection.
- `main.py:109-112`: validate or adjust only if ROCm training is in scope. It is not needed for the real-world inference entry point.

No `models/*.py` file requires a ROCm-specific source rewrite based on this audit. The logic/video defects above still require non-ROCm fixes before reliable inference.

## 5. Estimated change size

**Small for the vendor port.** Criteria: one ROCm-aware environment path, matched Torch/Torchvision wheels, and zero model-architecture changes; AMP remains enabled if Stage 3 passes. No custom kernel translation is indicated.

**Medium for runnable/reproducible inference.** Criteria: add an explicit ROCm lock/manifest, `numpy==1.26.4`, asset preflight/cache instructions, resolve Lightning 2.1.2/torchmetrics/CLIP compatibility, add device/AMP and video-output guards, fix the upstream reference/resize defects, and automate Stage 1-3 smoke tests.

**Large only if validation fails at the backend boundary.** Criteria: unsupported ROCm operators, CLIP TorchScript incompatibility that cannot be avoided by non-JIT mode, or a dependency requiring a HIP/custom-kernel rewrite. No evidence of that larger case exists in the checked-in source, and actual GPU success is unproven.

## 6. Checkpoint portability and caveats

Existing checkpoints are expected to load unchanged on ROCm when they are ordinary PyTorch state dictionaries; this remains **unverified locally** because the checkpoint and runtime are absent:

- `real_world_inference.py:147-151` constructs `SwinUNet`, calls `torch.load(..., map_location="cpu")`, loads with `strict=True`, then moves the model to the selected device.
- `main.py:44-54` uses the same CPU remapping; scratch Lightning checkpoints are unwrapped from `state_dict` and their `net.` prefix is stripped before strict loading. The pretrained path at `main.py:51-54` expects a bare state dictionary.

`map_location="cpu"` avoids deserializing tensors onto a missing NVIDIA device, and the subsequent `.to(device)` is backend-neutral. Therefore the weights themselves do not require CUDA-to-HIP conversion.

Caveats:

- The release checkpoint is absent and must be obtained from the `weights` release. Its exact key layout must pass the repository’s strict load.
- `SwinUNet()` constructs the pretrained torchvision Swin-T extractor before the TAPE checkpoint is loaded (`models/swin_unet.py:120-129`, `models/swin_feature_extractor.py:29`), so the torchvision weight cache/network requirement can fail first.
- `torch.load` does not specify `weights_only`; newer PyTorch releases changed that default. A bare tensor state dict is normally unaffected, but old Lightning checkpoints containing non-tensor metadata may need an explicit load policy in a later compatibility patch.
- The official CLIP `RN50x4.pt` asset is separate from the TAPE checkpoint and is needed by the real-world reference-selection phase.

## 7. Recommended WSL2 + ROCm PyTorch baseline

For the first controlled trial, use WSL2/Linux rather than native Windows Python:

- Use one exact AMD ROCm 7.2 wheel tuple, not a mixed environment:
  - Ubuntu 22.04 + Python 3.10 + `cp310` wheels; or
  - Ubuntu 24.04 + Python 3.12 + `cp312` wheels.
- Pin `numpy==1.26.4` for the AMD ROCm 7.2 environment.
- A Python virtual environment rather than assuming conda; AMD’s current ROCm WSL installation page recommends pip and says conda use cases are not officially validated.
- PyTorch 2.9.1 + ROCm 7.2 is a supported ROCm framework baseline, not a proven TAPE dependency set. AMD publishes matching torchvision 0.24.0 and Triton 3.5.1 wheels for both the `cp310` Ubuntu 22.04 path and the `cp312` Ubuntu 24.04 path.
- Install TAPE’s non-Torch pins only after the ROCm Torch pair is installed. Compatibility of `pytorch-lightning==2.1.2`, the unresolved `torchmetrics` version, and the pinned OpenAI CLIP package with this newer Torch/Torchvision stack remains **unresolved and unverified**.

AMD’s newer documentation also has a ROCm 10.0 branch and a current install selector. The exact RX 7900 XT + WSL2 + Python tuple, the `numpy==1.26.4` note for the selected wheel, and the user’s AMD Software 26.6.4 driver pairing must be rechecked in that selector before choosing ROCm 10.x. The explicit ROCm 7.2/2.9.1 tuple above is the current source-backed framework baseline found during this audit, not a claim that TAPE or every newer driver/ROCm combination is interchangeable.

Official references:

- [AMD WSL compatibility matrix](https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2/docs/compatibility/compatibilityrad/wsl/wsl_compatibility.html): RX 7900 XT, Ubuntu 22.04/24.04, and PyTorch 2.9.1/ROCm 7.2.
- [AMD Install PyTorch for ROCm on WSL](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/install/installrad/wsl/legacywsl/install-pytorch.html): pip recommendation and published cp310 ROCm wheels.
- [PyTorch Start Locally](https://docs.pytorch.org/get-started/locally/): choose Linux/Pip/ROCm and verify with `torch.cuda.is_available()`; it explicitly notes that ROCm PyTorch uses the same Python API semantics.
- [TAPE pretrained-model release](https://github.com/miccunifi/TAPE/releases/tag/weights).

## 8. Minimal staged validation procedure

These are proposed commands only; they were **not run** in Phase 1. Use a copied/isolated output directory and never overwrite the source video.

### Stage 1 — PyTorch sees the RX 7900 XT

Inside WSL2, after the ROCm environment is installed:

```bash
python -c "import torch; print('torch=', torch.__version__); print('hip=', torch.version.hip); print('available=', torch.cuda.is_available()); print('count=', torch.cuda.device_count()); print('name=', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'N/A'); assert torch.version.hip is not None; assert torch.cuda.is_available()"
```

Evidence: Torch version, non-`None` HIP version, `available=True`, device count, and a name identifying the RX 7900 XT. Do not treat the `torch.cuda` spelling as failure.

### Stage 2 — actual checkpoint load and GPU transfer only

After placing the release checkpoint and caching required torchvision weights:

```bash
cd /mnt/e/vhs-upscale/TAPE
python -c "import torch; from models.swin_unet import SwinUNet; p='experiments/pretrained_model/checkpoint.pth'; s=torch.load(p, map_location='cpu'); m=SwinUNet(); m.load_state_dict(s, strict=True); m.eval().to('cuda'); print('loaded_keys=',len(s)); print('device=',next(m.parameters()).device); print('vram=',torch.cuda.memory_allocated())"
```

Stage 2 evidence is successful construction, strict load, GPU transfer, and no OOM; no forward pass is performed. The official real-world checkpoint is expected to be a bare state dictionary; Lightning-wrapper handling is only in the scratch/evaluation path (`main.py:44-54`).

### Stage 3 — actual checkpoint forward, CLIP, and a short real clip

First load the actual TAPE checkpoint again and run a finite forward on small synthetic tensors. This must assert output shape and finiteness:

```bash
cd /mnt/e/vhs-upscale/TAPE
python - <<'PY'
import torch
from models.swin_unet import SwinUNet

device = torch.device("cuda")
state = torch.load("experiments/pretrained_model/checkpoint.pth", map_location="cpu")
model = SwinUNet()
model.load_state_dict(state, strict=True)
model.eval().to(device)
x = torch.zeros((1, 5, 3, 128, 128), device=device)
r = torch.zeros((1, 5, 3, 128, 128), device=device)
torch.cuda.reset_peak_memory_stats()
with torch.inference_mode(), torch.cuda.amp.autocast():
    y = model(x, r)
assert y.shape == (1, 5, 3, 128, 128)
assert torch.isfinite(y).all().item()
print("shape=", tuple(y.shape), "dtype=", y.dtype,
      "peak_vram=", torch.cuda.max_memory_allocated())
PY
```

Then exercise the external CLIP path separately, including the actual `RN50x4` model and text/image encoders:

```bash
cd /mnt/e/vhs-upscale/TAPE
python - <<'PY'
import torch
import clip
from PIL import Image

device = torch.device("cuda")
model, preprocess = clip.load("RN50x4", device=device, jit=True)
with torch.inference_mode(), torch.cuda.amp.autocast():
    text = clip.tokenize(["a clean video frame"]).to(device)
    image = preprocess(Image.new("RGB", (224, 224))).unsqueeze(0).to(device)
    text_features = model.encode_text(text)
    image_features = model.encode_image(image)
assert torch.isfinite(text_features).all().item()
assert torch.isfinite(image_features).all().item()
print("clip=ok", "text=", tuple(text_features.shape),
      "image=", tuple(image_features.shape))
PY
```

Finally run the complete pipeline on a copied, very short real clip (for example 1-2 seconds) before Stage 4:

```bash
python real_world_inference.py --input-path /path/to/short_static_clip.mkv --output-path /path/to/tape_stage3_out --checkpoint-path experiments/pretrained_model/checkpoint.pth --batch-size 1 --num-workers 0
```

Stage 3 evidence: strict checkpoint load, finite `(1, 5, 3, 128, 128)` output, finite CLIP encodings, and a successful short-clip output. If AMP fails, rerun the model and CLIP smoke tests in FP32 and record the result separately; do not infer that the model requires a custom HIP port.

### Stage 4 — 5-10 seconds

Run the unmodified entry point against a short copied sample:

```bash
python real_world_inference.py --input-path /path/to/sample_5_10s.mkv --output-path /path/to/tape_stage4_out --checkpoint-path experiments/pretrained_model/checkpoint.pth --batch-size 1 --num-workers 2
```

Record end-to-end time, frames/FPS, output dimensions/fps, errors, and peak VRAM. Keep input and output paths separate.

### Stage 5 — 30-60 seconds

Repeat Stage 4 with a longer sample while recording processing FPS, peak VRAM, GPU utilization, CPU utilization, output integrity, and visual artifacts. Use the host’s supported AMD monitoring tool (`amd-smi`/`rocm-smi`, whichever the selected WSL stack provides) without changing the source video.

## Commands/evidence used for this audit

```text
git clone https://github.com/miccunifi/TAPE.git E:\vhs-upscale\TAPE
git -C E:\vhs-upscale\TAPE rev-parse HEAD
git -C E:\vhs-upscale\TAPE symbolic-ref --short refs/remotes/origin/HEAD
git -C E:\vhs-upscale\TAPE show-ref --heads --tags
git -C E:\vhs-upscale\TAPE status --short --branch
git -C E:\vhs-upscale\TAPE ls-files
rg --files E:\vhs-upscale\TAPE
rg -n -i --glob '!**/.git/**' '(cuda|hip|nvidia|cudnn|apex|xformers|cupy|nvcc|\.cu|\.cpp|torch\.utils\.cpp_extension|triton|nccl)' E:\vhs-upscale\TAPE
rg -n --glob '*.py' '(torch\.cuda|torch\.load|load_state_dict|checkpoint|map_location|autocast|device=|\.to\(device|pin_memory|non_blocking)' E:\vhs-upscale\TAPE
```

No tests, dependency installs, checkpoint downloads, GPU checks, inference, or input-video access were performed. The only created report artifact is this file; the upstream checkout remains unmodified.
