# 実装計画書: QTGMC・長時間ローカル処理

> 状態: 最終承認済み  
> 作成日: 2026-09-20  
> 最終承認日: 2026-09-20  
> 設計根拠: `docs/superpowers/specs/2026-09-14-rx7900xt-tape-rocm-design.md`  
> 詳細テスト: `@./test_cases.md`  
> 独立レビュー: `cursor/glm-5.2`（Critical 0、Important 8。全必須指摘を反映）
> 注意(2026-09-22 構造修正): 本計画 §2 の「実装対象repositoryはTAPE worktreeである」は、設計書の repository root 規定に従い **VHS-upscale ルート(`E:/vhs-upscale`)へ置き換え済み**。実施詳細は `docs/qtgmc-long-processing/implementation_record.md` の "Structural correction record (2026-09-22)" を参照。TAPE worktree は TAPE 側 patch のみ(ローカル保持)。本計画の各Task定義・gate・完了条件は有効。


## 1. 目的

Windows側VapourSynth/QTGMCで29.97i VHSをlosslessなprogressive動画へ変換し、WSL2/RX 7900 XT上のTAPEへ渡す。TAPEは動画単位の共通reference cacheを使ってframe chunkごとに処理し、失敗chunkだけを再実行できるようにする。最終成果物はTAPE restored FFV1 videoとoriginal audioを含むMatroskaとし、Modal RealBasicVSRへ渡せるmanifestを併置する。

この計画は「実装者が設計判断を追加しない」ことを重視する。各Taskに変更file、公開interface、先に書くtest、実行command、完了条件、停止条件を固定する。

## 2. 現在地と実装開始点

実装対象repositoryはTAPE worktreeである。計画作成時の完成済みbaselineは次のとおり。

- branch: `codex/tape-rocm-rx7900xt-luna`
- commit: `c4ee9aae6e95ab257e3b3e4b538e6c20e1b7c9dc`
- upstream base: `fb97c608a1733dec5dbcd5aeb77ed84e29b793f7`
- 既存short runner: `scripts/run_short_inference.py`
- 既存TAPE entrypoint: `real_world_inference.py`
- 既存media contract tests: `tests/test_short_inference_contract.py`
- 実測: 704×396、30p、297 framesを282.633秒、1.051 fps
- 実測peak: allocated 8.81 GiB、reserved 14.56 GiB、AMP/autocast
- 1280×720 full-resolution TAPEは現状のVRAM envelopeを超えてfail closed
- Windows: Python 3.13、VapourSynth R79、vspipe、FFmpeg 8.1.1
- `vsjetpack==2.2.4`は存在するが、現時点のplugin inventoryにはQTGMC実行に必要なnative pluginが揃っていない
- `input/`にある確認済み素材はprogressiveであり、QTGMC実機受入に使える29.97i sourceはまだ無い

実装開始時に次を実行し、`baseline.json`へ記録する。新規worktreeは必ず`c4ee9aae6e95ab257e3b3e4b538e6c20e1b7c9dc`を直接baseにする。上記commitが既に統合済みの場合だけ、そのcommitを祖先に持つ統合先commitをbaseとしてよい。その場合も`git merge-base --is-ancestor`の成功を記録する。dirty worktreeなら続行しない。

実装は`E:\vhs-upscale\worktrees\tape-qtgmc-long-processing`という新しいGit worktreeで開始する。この計画内のsource pathは、特記がなければそのworktree rootからの相対pathである。

```powershell
git status --short
git rev-parse HEAD
git merge-base --is-ancestor c4ee9aae6e95ab257e3b3e4b538e6c20e1b7c9dc HEAD
python -m pytest -m "not gpu and not integration" -v
```

## 3. スコープ

### 3.1 対象

- Windows VapourSynth環境のversion/plugin preflight
- `vsdeinterlace.QTempGaussMC`によるTFF/BFF対応QTGMC
- `raw`、`light`、`strong`のpreview候補
- QTGMC 59.94p維持と、決定論的29.97p化の比較
- FFV1 `yuv422p10le`とoriginal audio stream copy
- stage manifest、artifact hash、設定変更に応じたinvalid化
- 動画単位CLIP feature shard、clean reference pool、全chunk共通mapping
- frame range based TAPE chunk、overlap、trim、concat
- 失敗chunkだけを再実行するresume
- 複数動画を順番に処理するbatch
- runtime、disk、TAPE FPS、peak VRAM、error分類の記録
- 30～60秒canary後のVHS一本処理
- Modal RealBasicVSRへ渡すhandoff manifest

### 3.2 対象外

- Modal、RealBasicVSR、upload/download、課金計測
- GUI
- QTGMC stage内部のchunk resume
- feature shard内部のframe単位resume。v1の再開粒度はshard単位とする
- 複数TAPE GPU workerの並列実行
- TAPE architecture、checkpoint、学習codeの変更
- source metadataの推測補完
- JPEG intermediate
- 1280×720 TAPEを無理に通すためのmodel rewrite

QTGMCはtemporal filterのため、v1ではsource全体を1本のFFV1へ原子的に出力する。完成artifactはresumeでskipできるが、QTGMC途中frameからの再開は行わない。chunk resumeはQTGMC後のTAPE stageへ限定する。

## 4. 採用方式と不採用方式

### 4.1 採用: stage artifact + TAPE frame chunk

Windows orchestratorがanalyze、QTGMC、preview、最終検証を管理し、WSL workerがreference featureとTAPE inferenceを実行する。両環境は`E:\vhs-upscale\work`を共有する。各stageはsignatureとartifact SHA-256を持ち、一致したcompleted stageだけskipする。

採用理由:

- 現行TAPEは全動画展開と動画単位reference選択を一体で行うため、長時間処理へ直接使えない。
- TAPE temporal windowはframe範囲へ明示的に分解できる。
- reference poolを動画単位で固定すればchunk境界の復元傾向を揃えられる。
- FFV1 chunkは検証、再実行、concatを独立して行える。

### 4.2 不採用: 動画全体を1回のTAPEへ渡す

大量PNG、長い失敗回復時間、既存output directory衝突、reference再計算のため不採用。

### 4.3 不採用: QTGMCも短いchunkへ分割する

QTGMCのmotion analysisとtemporal smoothingの境界差をv1で安全に証明できないため不採用。将来必要なら、同じsourceをfull renderとchunk renderの両方で処理し、境界前後のframe hashまたは許容差付き画素差を比較するseam testを必須とする別specで扱う。

### 4.4 不採用: chunkごとにCLIP referenceを選ぶ

chunk境界ごとにclean candidateと復元傾向が変わるため不採用。

## 5. 固定した技術判断

1. QTGMC implementationは`havsfunc.QTGMC`ではなく`vsdeinterlace.QTempGaussMC`を使う。現在の`havsfunc==34`は`QTGMC`をexportしていない。
2. Windows dependencyは`vapoursynth==79`、`vsjetpack[deinterlace,source]==2.2.4`をbaselineとし、native wheel用extra indexをrequirementsへ固定する。
3. plugin preflightはimportだけで終えず、synthetic clipのframe requestまで行う。VapourSynth pluginはlazy loadだからである。
4. source decodeは`vssource.BestSource().source(path)`を使い、index cacheはrun directory配下へ置く。
5. auto field-order判定は`tt -> TFF`、`bb -> BFF`だけを許可する。`tb`、`bt`、unknown、欠損は明示overrideが必要。progressive入力はQTGMC対象として拒否する。
6. 59.94pから29.97pへの比較経路は`SelectEvery(2, 0)`で偶数indexを残す。補間や平均化はしない。
7. subprocessはすべてargument listで起動する。shell文字列、`shell=True`、unquoted path連結は禁止する。WSLも`wsl.exe -d <distro> -- <absolute-wsl-python> <absolute-wsl-script> <args>...`という純粋なargvで起動し、`bash -lc`を使わない。環境変数が必要なら`env NAME=VALUE ...`をargvとして渡す。
8. 正式artifactは`.partial`へ作成し、contract PASS後に同一volumeの`os.replace`でpublishする。
9. manifestとcache keyはcanonical JSON SHA-256で識別する。
10. TAPE input/restored frame名は9桁global frame indexにする。chunk内0始まりへrenumberしない。
11. `num_input_frames=5`のdefault overlapは2 frames。2未満を自動補正せず拒否する。
12. reference pool defaultは256 frames、reference数は5、feature dtypeはfloat32、feature shardは10,000 frames。
13. CLIP prompt ensemble、feature normalize、Otsu clean scoreの計算は既存挙動を維持する。ただしreference選択だけは、既存のclean candidate全件に対するall-pairs方式から、timeline binごとの固定最大256 pool方式へ意図的に変更する。
14. TAPE GPU workerの並列数は常に1。
15. 成功したchunkだけPNGを削除してよい。失敗chunkのdiagnosticは保持する。
16. 最終videoはFFV1 level 3、GOP 1、slice CRC、`yuv422p10le`。長時間pipelineは1本以上のaudio streamを許可し、original sourceの全audio streamをstream copyする。既存short runnerの「audioはちょうど1本」という契約は変更しない。
17. 10-bit FFV1をCLIP featureへ渡す箇所はFFmpeg `rgb24`で8-bit化される。raw byteを`uint8[h,w,3]`へreshapeし、RGB順のまま`PIL.Image.fromarray`を経て既存`clip_preprocess`へ渡す。BGR変換は禁止する。pool PNGとTAPE PNGも8-bitであり、10-bit FFV1は失われた精度を復元しないことをreportへ必ず書く。
18. repository内VapourSynth scriptは`vs_scripts/`へ置く。installed `vapoursynth` Python moduleをshadowする`vapoursynth/` directoryは作らない。
19. 長時間reference poolが0件なら`reference_failed`で停止する。既存の空pool fallbackまたは`resolve_reference_names`によるpaddingはshort mode専用とし、long modeで呼ばない。
20. chunk encodeとconcat/remuxはanalyze時に解決した同一のWindows FFmpeg executableを使う。path、version、CFR指定をmanifest/sidecarへ記録する。
21. `source-hash12`はsource SHA-256 lower-case hexの先頭12文字とする。`source.stem`が正規表現`[A-Za-z0-9][A-Za-z0-9._-]{0,79}`へ完全一致するときだけその値をsafe stemに使う。それ以外は`source-<source-hash12>`とする。run directoryと正式成果物名には常に`source-hash12`を含める。
22. 正式な最終成果物は`E:\vhs-upscale\work\tape\<safe-stem>-<source-hash12>_tape.mkv`と隣接handoff JSONである。run-localな中間物は`work\local-pipeline`に残す。

VapourSynth R74以降のpip/plugin配置とVSRepoの扱いは[公式installation guide](https://www.vapoursynth.com/doc/installation.html)、vsjetpack extrasは[vs-jetpack公式repository](https://github.com/Jaded-Encoding-Thaumaturgy/vs-jetpack)、QTempGaussMC APIは[公式source](https://github.com/Jaded-Encoding-Thaumaturgy/vs-jetpack/blob/main/vsdeinterlace/qtgmc.py)を根拠にする。

## 6. 実装後のdirectory構成

以下のpathはTAPE implementation worktree rootからの相対pathである。

```text
requirements-vapoursynth.txt
pytest.ini
vs_scripts/
  __init__.py
  qtgmc.py
  render_qtgmc.vpy
pipeline/
  __init__.py
  canonical.py
  chunks.py
  manifest.py
  media_contract.py
  paths.py
  references.py
  tape_chunk.py
  run_local.py
scripts/
  check_vapoursynth.py
  install_vapoursynth.ps1
tests/
  test_qtgmc_contract.py
  test_chunks.py
  test_manifest.py
  test_reference_cache.py
  test_tape_chunk.py
  test_long_media_contract.py
  test_run_local.py
docs/
  qtgmc-long-processing.md
```

Runtime artifact:

```text
E:\vhs-upscale\work\local-pipeline\<safe-stem>-<source-hash12>\
  manifest.json
  logs\
  analyze\source.ffprobe.json
  previews\preview_matrix.json
  previews\selection.json
  preprocessed\<preset>-<rate>.mkv
  references\<cache-key>\manifest.json
  references\<cache-key>\features\*.npz
  references\<cache-key>\pool\*.png
  references\<cache-key>\mapping.json
  tape\chunks\000000\...
  tape\video_only.mkv
  final\video_only.verified.mkv
  final\verification.json
E:\vhs-upscale\work\tape\
  <safe-stem>-<source-hash12>_tape.mkv
  <safe-stem>-<source-hash12>_handoff.json
  <safe-stem>-<source-hash12>_run_manifest.json
  <safe-stem>-<source-hash12>_chunk_manifest.json
  <safe-stem>-<source-hash12>_reference_manifest.json
```

## 7. component責務と公開interface

### 7.1 `vs_scripts/qtgmc.py`

```python
@dataclass(frozen=True)
class QTGMCConfig:
    preset: Literal["raw", "light", "strong"]
    field_order: Literal["tff", "bff"]
    fps_mode: Literal["double", "half"]

def preset_parameters(name: str) -> Mapping[str, object]: ...
def build_qtgmc_clip(source: vs.VideoNode, config: QTGMCConfig) -> vs.VideoNode: ...
def validate_qtgmc_output(source: vs.VideoNode, output: vs.VideoNode, config: QTGMCConfig) -> None: ...
```

`raw/light/strong`はversioned constantとして定義する。実装者はparameterをその場で調整しない。初期mappingは次で開始し、VapourSynth integration testがAPI不一致を示した場合だけsourceに合わせて名前を修正する。

ここで`raw`は「interlaced sourceを未処理で通す」という意味ではない。QTempGaussMC defaultだけを使う比較baselineを意味する。全候補はdeinterlace済みprogressiveでなければならない。

```python
PRESETS = {
    "raw": {},
    "light": {
        "denoise_tr": 1,
        "source_match_mode": QTempGaussMC.SourceMatchMode.BASIC,
    },
    "strong": {
        "denoise_tr": 2,
        "source_match_mode": QTempGaussMC.SourceMatchMode.REFINED,
    },
}
```

`fps_mode=double`は`QTempGaussMC(**params).deinterlace(source, tff=...)`の出力をそのまま使う。`half`はその出力へ`std.SelectEvery(cycle=2, offsets=0)`を適用する。frame count/rate assertionが外れたらencodeへ進まない。

### 7.2 `pipeline/chunks.py`

```python
@dataclass(frozen=True)
class ChunkRange:
    index: int
    core_start: int
    core_end: int
    decode_start: int
    decode_end: int
    trim_left: int
    trim_right: int

def seconds_to_frames(seconds: Decimal, fps: Fraction) -> int: ...
def plan_chunks(total_frames: int, core_frames: int, overlap: int, num_input_frames: int) -> tuple[ChunkRange, ...]: ...
def validate_partition(chunks: Sequence[ChunkRange], total_frames: int) -> None: ...
```

range式は次から変更しない。

```text
core_start = index * core_frames
core_end = min(total_frames, core_start + core_frames)
decode_start = max(0, core_start - overlap)
decode_end = min(total_frames, core_end + overlap)
trim_left = core_start - decode_start
trim_right = decode_end - core_end
```

### 7.3 `pipeline/manifest.py`

```python
class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    INVALID = "invalid"

def stage_signature(stage: str, inputs: Mapping, config: Mapping, versions: Mapping) -> str: ...
def load_manifest(path: Path) -> RunManifest: ...
def write_manifest_atomic(path: Path, manifest: RunManifest) -> None: ...
def decide_stage(manifest: RunManifest, stage: str, signature: str) -> Literal["skip", "run"]: ...
def invalidate_from(manifest: RunManifest, changed_keys: set[str]) -> list[str]: ...
```

schema versionは1から開始する。unknown future versionを読まない。`complete`判定はstatus文字列だけでなくartifact hashとcontract結果を再検証する。

### 7.4 `pipeline/references.py`

```python
@dataclass(frozen=True)
class ReferenceConfig:
    model_sha256: str
    prompt_version: str
    pool_algorithm_version: str
    pool_size: int = 256
    references_per_frame: int = 5
    shard_frames: int = 10_000
    feature_dtype: str = "float32"

def reference_cache_key(video_manifest: Mapping, config: ReferenceConfig) -> str: ...
def plan_feature_shards(total_frames: int, shard_frames: int) -> tuple[FrameRange, ...]: ...
def frame_bytes_to_pil_rgb(data: bytes, width: int, height: int) -> Image.Image: ...
def choose_reference_pool(frame_indices: ndarray, clean_scores: ndarray, pool_size: int) -> ndarray: ...
def build_reference_mapping(features: ndarray, pool_indices: ndarray, count: int) -> dict[str, list[str]]: ...
def validate_reference_cache(root: Path, expected_key: str) -> ReferenceCacheManifest: ...
```

feature抽出は既存`real_world_inference.py`のCLIP prompt ensemble、normalize、Otsu clean scoreをhelperへ移す。数式・prompt内容・assetは変更しない。FFmpeg `rgb24`の1 frameを正確に`width * height * 3` byte読み、`np.frombuffer(..., dtype=np.uint8).reshape(height, width, 3)`、`Image.fromarray(array, mode="RGB")`の順で既存`clip_preprocess`へ渡す。OpenCV BGR順への変換は行わない。

reference選択だけは意図的な新algorithmである。既存のclean candidate全件all-pairs比較は使わず、timeline binごとのclean score最良frameから最大256件の固定poolを作り、target shardとpoolのcosine similarityだけを計算する。全frame featureを10,000frame shardへ保存し、各shardをhash付きでresumeする。v1はshard内部から再開せず、失敗shard全体を再計算する。pool PNGだけを恒久保存し、全動画PNGは保存しない。pool 0件はfallbackせず失敗にする。

### 7.5 `real_world_inference.py`とdataset変更

追加CLI:

```text
--input-frames-path PATH
--reference-map-path PATH
--reference-frames-path PATH
--skip-reference-selection
--global-start-frame INT
```

external modeの契約:

- process開始時に、external用5引数がすべて存在するか、すべて存在しないかでmodeを一度だけ確定する。
- `--input-frames-path`のPNGをそのまま使い、OpenCV動画decodeをしない。
- `--skip-reference-selection`時はCLIPをloadしない。
- external modeではframe extraction、CLIP/Otsu、reference map生成をすべてskipする。
- mapping keyとinput PNG名はglobal 9桁frame名。
- reference値は`--reference-frames-path`から読む。
- model、checkpoint load、preprocess、PNG write、metricsは既存経路を再利用する。
- external引数の一部だけ指定された場合はparser error。
- 既存short runner modeを壊さない。

`RealWorldVideoDataset`へ`reference_frames_folder`を追加し、未指定時は従来どおりinput folderを使う。これ以外のmodel data flowは変更しない。

### 7.6 `pipeline/tape_chunk.py`

```python
def extract_chunk_frames(video: Path, chunk: ChunkRange, output: Path, fps: Fraction) -> list[Path]: ...
def run_tape_chunk(request: TapeChunkRequest, command_runner: CommandRunner) -> ChunkResult: ...
def trim_to_core(restored_root: Path, chunk: ChunkRange) -> list[Path]: ...
def encode_chunk_ffv1(frames: Sequence[Path], media: MediaManifest, partial: Path) -> None: ...
def validate_chunk(result: ChunkResult, request: TapeChunkRequest) -> None: ...
```

frame抽出はglobal indexを名前に使う。workerはdecode rangeを処理し、core rangeだけをchunk FFV1へencodeする。chunk sidecarへcore/decode range、frame hash list、reference cache key、checkpoint hash、metrics、stderr log pathを保存する。

WSL起動argvは次の形から変えない。`--tape-worktree`を先にabsolute WSL pathへ変換し、Python、script、input、mapping、reference、outputの各pathをそれぞれ1つのargv elementとして渡す。

```text
["wsl.exe", "-d", distro, "--", absolute_wsl_python, absolute_wsl_script,
 "--input-frames-path", absolute_wsl_input, "--reference-map-path", absolute_wsl_map,
 "--reference-frames-path", absolute_wsl_references, "--skip-reference-selection", ...]
```

`bash`、`-lc`、command文字列の組立ては使用しない。

### 7.7 `pipeline/run_local.py`

subcommand:

```text
analyze
preview
select
canary
run
resume
verify
batch
status
```

共通必須引数:

```text
--input PATH
--work-root PATH
--checkpoint PATH
--tape-worktree PATH
--wsl-distro Ubuntu-22.04
```

`preview`は6候補を生成する。`select`はselection JSONだけを更新する。`canary`と`run`はvalid selectionなしで停止する。`resume`は同じrun idを読み、`failed/interrupted/invalid`だけを再実行する。`batch`は入力ごとに独立manifestを持ち、GPU処理は直列にする。

path生成は`safe_source_stem(source, source_hash12)`だけを経由する。concat listへ書く各chunk pathはresolve後にtrusted run root配下であることを確認し、CR、LF、単一quoteを含むpathは拒否する。

## 8. Manifest schema

実装者は次のtop-level keyを削除・改名しない。

```json
{
  "schema_version": 1,
  "run_id": "sha256...",
  "created_at": "RFC3339 UTC",
  "updated_at": "RFC3339 UTC",
  "source": {
    "path": "absolute Windows path",
    "safe_stem": "ASCII-safe stem",
    "size": 0,
    "mtime_ns": 0,
    "sha256": "...",
    "fingerprint": "...",
    "ffprobe_artifact": "relative path",
    "media": {},
    "overrides": {}
  },
  "selection": {
    "preview_matrix_sha256": "...",
    "qtgmc_preset": "light",
    "tape_input_rate": "60000/1001"
  },
  "assets": {
    "tape_checkpoint_sha256": "...",
    "clip_sha256": "...",
    "swin_sha256": "..."
  },
  "config": {
    "core_seconds": "300",
    "overlap_frames": 2,
    "num_input_frames": 5,
    "num_reference_frames": 5,
    "reference_pool_size": 256,
    "feature_shard_frames": 10000,
    "patch_size": 512,
    "precision": "amp_autocast"
  },
  "versions": {
    "windows_ffmpeg_path": "absolute path",
    "windows_ffmpeg_version": "...",
    "ffmpeg_cfr_policy": "..."
  },
  "publish": {
    "final_mkv": "E:\\vhs-upscale\\work\\tape\\<safe-stem>-<source-hash12>_tape.mkv",
    "handoff_json": "adjacent path",
    "run_manifest_snapshot": "adjacent path",
    "chunk_manifest_snapshot": "adjacent path",
    "reference_manifest_snapshot": "adjacent path"
  },
  "stages": {
    "analyze": {},
    "qtgmc": {},
    "references": {},
    "tape_chunks": {},
    "concat": {},
    "remux": {},
    "verify": {}
  },
  "problems": []
}
```

各stage recordは最低限次を持つ。

```json
{
  "status": "pending|running|complete|failed|interrupted|invalid",
  "signature": "sha256...",
  "attempt": 0,
  "started_at": null,
  "completed_at": null,
  "inputs": [],
  "artifacts": [
    {"path": "relative path", "size": 0, "sha256": "..."}
  ],
  "contract_passed": false,
  "metrics": {},
  "error": null
}
```

## 9. invalid化DAG

実装は文字列比較のifを散在させず、1つのdependency tableからinvalid化する。

```text
analyze
  -> qtgmc
     -> references
        -> tape_chunks
           -> concat
              -> remux
                 -> verify
```

例外:

- TAPE checkpoint、patch size、precisionは`references`をinvalid化せず`tape_chunks`以降だけ。
- CLIP hash、prompt、pool設定は`qtgmc`を保持し`references`以降。
- mux設定は`concat/remux/verify`だけ。
- report表示変更はartifact stageをinvalid化しない。

詳細期待値は`@./test_cases.md` TC-17～TC-21を正本とする。

## 10. 実装手順

### Task 0: baselineを固定する

**Files:**

- Create: `docs/qtgmc-long-processing.md`
- Create outside Git: `work/local-pipeline/baseline.json`

**Steps:**

1. clean worktreeを確認する。
2. HEAD、branch、Python/VapourSynth/vspipe/ffmpeg、Windows、WSL、TAPE asset hashをJSONへ記録する。
3. 既存unit、GPU、integration testを各groupでfresh実行する。
4. `docs/qtgmc-long-processing.md`へ実測baselineと既知制約を転記する。
5. commit: `docs: record QTGMC pipeline baseline`

**完了条件:** 既存testが全PASSし、予期しないskipがない。1280×720 OOMと704×396実測を隠さず記録する。

**停止条件:** dirty worktree、既存test failure、asset hash不一致。修正を混ぜず`superpowers:systematic-debugging`へ切り替える。

---

### Task 1: package skeletonとcanonical helper

**Files:**

- Create: `pipeline/__init__.py`
- Create: `pipeline/canonical.py`
- Create: `pipeline/paths.py`
- Create: `tests/test_manifest.py`

**Tests first:** TC-15のcanonical JSON部分、TC-28のWindows pathからWSL pathへの変換と純argv、TC-33のsafe stem/concat path、unsupported UNC/path拒否。

**Required implementation:**

- `canonical_json_bytes(value) -> bytes`
- `sha256_file(path) -> str`
- `sha256_json(value) -> str`
- `windows_to_wsl(path, drive_map={"E:": "/mnt/e"}) -> PurePosixPath`
- `safe_source_stem(source: Path, source_hash12: str) -> str`
- `build_wsl_argv(distro, python, script, args) -> list[str]`
- hash readは1 MiB chunk。
- JSONは`sort_keys=True,separators=(",", ":"),ensure_ascii=False`。
- WSL argvは`bash -lc`やshell command文字列を含めず、absolute WSL pathを各1 elementへ分離する。

**RED:** `python -m pytest tests/test_manifest.py -v` がmodule不存在でFAIL。

**GREEN:** 同command全PASS。

**Commit:** `feat: add canonical pipeline primitives`

---

### Task 2: manifest state machineとinvalid化DAG

**Files:**

- Create: `pipeline/manifest.py`
- Extend: `tests/test_manifest.py`

**Tests first:** TC-15～TC-21。

**Required implementation:**

- schema v1 dataclassまたはTypedDict。
- atomic JSON write: sibling temp、flush、`os.fsync`、`os.replace`。
- startup reconciliation: `running -> interrupted`。
- completed artifactの存在、size、hash、contract再検証。
- dependency tableによるdownstream invalid化。
- invalid artifactは削除せずrenameして隔離。

**禁止:** statusがcompleteという理由だけでskip、hash mismatchの自動上書き、unknown schemaのbest-effort読込。

**RED/GREEN:** `python -m pytest tests/test_manifest.py -v`

**Commit:** `feat: add atomic stage manifest and resume rules`

---

### Task 3: frame chunk planner

**Files:**

- Create: `pipeline/chunks.py`
- Create: `tests/test_chunks.py`

**Tests first:** TC-11～TC-14。

**Required implementation:** section 7.2の式をそのまま実装し、core partitionを別validatorで検査する。fpsは`fractions.Fraction`、secondsは`Decimal`で扱う。

**RED/GREEN:** `python -m pytest tests/test_chunks.py -v`

**Commit:** `feat: add deterministic frame chunk planner`

---

### Task 4: Windows VapourSynth環境を再現可能にする

**Files:**

- Create: `requirements-vapoursynth.txt`
- Create: `scripts/install_vapoursynth.ps1`
- Create: `scripts/check_vapoursynth.py`
- Update: `pytest.ini`
- Create: `tests/test_qtgmc_contract.py`

**requirements content:**

```text
--extra-index-url https://jaded-encoding-thaumaturgy.github.io/vs-wheels/simple
vapoursynth==79
vsjetpack[deinterlace,source]==2.2.4
```

**Installer rules:**

- defaultは変更内容を表示するdry-run。
- `-Apply`指定時だけpip installする。
- system-wide uninstall、既存Python削除、registry変更をしない。
- 実行したcommandとexit codeをlogへ保存する。

**Preflight rules:**

- TC-01を実装する。
- plugin listだけでなくQTempGaussMC synthetic first-frame requestを行う。
- `BestSource` pluginは短いfixtureを開いて検証する。

**RED:** plugin不足の現在環境で`python scripts/check_vapoursynth.py --json ...`が説明付きexit 1。これは期待されたREDである。

**Environment action:** ユーザー承認のある実装実行時だけ`install_vapoursynth.ps1 -Apply`を使う。

**GREEN:** preflight exit 0、`python -m pytest -m vapoursynth tests/test_qtgmc_contract.py -v` PASS。

**Commit:** `build: pin Windows VapourSynth QTGMC environment`

---

### Task 5: media analyzeとfield/color contract

**Files:**

- Create: `pipeline/media_contract.py`
- Create: `tests/test_long_media_contract.py`
- Reuse from: `scripts/run_short_inference.py`

**Tests first:** TC-02、TC-03、TC-08のmetadata部分、TC-34～TC-36。

**Required implementation:**

- `ffprobe_json`、`count_frames`、stream単位の`audio_packet_counts`、rational parsingだけを既存runnerから共通moduleへ移す。
- short用preflightとlong用preflightを分離する。既存short runnerはaudio streamがちょうど1本でなければ拒否する挙動を維持する。long pipelineはaudio streamが1本以上なら受理し、全streamのindex、codec、packet数をmanifestへ記録する。
- 共通moduleへ「audioは1本」というpolicyを置かない。policyは各runnerから明示的に選ぶ。
- field order fail-closed mapping。
- media override JSON schemaとprovenance。
- input hash/size/mtimeのbefore/after検証。

**Regression:** 既存`tests/test_short_inference_contract.py`を必ず同時に実行する。

```powershell
python -m pytest tests/test_long_media_contract.py tests/test_short_inference_contract.py -v
```

**Commit:** `refactor: share fail-closed media contracts`

---

### Task 6: QTGMC graphとvspipe render

**Files:**

- Create: `vs_scripts/__init__.py`
- Create: `vs_scripts/qtgmc.py`
- Create: `vs_scripts/render_qtgmc.vpy`
- Extend: `tests/test_qtgmc_contract.py`

**Tests first:** TC-04～TC-08。

**Required implementation:**

1. `BestSource().source(input)`でvideoを開く。
2. source/frame propertyとCLI field orderを照合する。
3. section 7.1のpreset registryからQTempGaussMC graphを作る。
4. output frame count/rate/progressiveをassertする。
5. vspipe stdoutをshellを介さずffmpeg stdinへ接続する。
6. FFV1 video-only partialをencodeする。
7. second ffmpeg passでoriginal audio全streamをcopyする。
8. contract PASS後だけpublishする。

**Exact CLI shape:**

```powershell
vspipe --y4m --arg input=E:\path\source.mkv --arg preset=light --arg field_order=tff --arg fps_mode=double vs_scripts\render_qtgmc.vpy -
```

PythonがPopen同士をpipe接続するため、上記をPowerShell pipelineとして実行しない。

**RED/GREEN:**

```powershell
python -m pytest -m "not vapoursynth" tests/test_qtgmc_contract.py -v
python -m pytest -m vapoursynth tests/test_qtgmc_contract.py -v
```

**Commit:** `feat: add atomic QTGMC FFV1 rendering`

---

### Task 7: preview matrixとhuman selection gate

**Files:**

- Create: `pipeline/run_local.py`の`analyze/preview/select/status`
- Create: `tests/test_run_local.py`
- Update: `docs/qtgmc-long-processing.md`

**Tests first:** TC-09、TC-10、TC-37前半。

**Required behavior:**

- `preview`は明示された各代表rangeについて6候補を作る。
- defaultで最低3 rangeを要求する。推奨labelは`motion`、`face`、`noise`。
- preview rangeはsource timestampとframe rangeの両方をmanifestへ保存する。
- TAPE previewは既存short runnerを再利用してよい。
- `selection.json`が無ければcanary/full runを拒否する。
- 選択は画質の自動metricで決めない。userがcandidateを見て指定する。

**Commands:**

```powershell
python -m pipeline.run_local analyze --input <29.97i-source> --work-root E:\vhs-upscale\work\local-pipeline ...
python -m pipeline.run_local preview --range motion=00:02:00,10 --range face=00:12:30,10 --range noise=00:25:00,10 ...
python -m pipeline.run_local select --preset light --tape-input-rate 60000/1001 ...
```

**Commit:** `feat: add QTGMC preview selection gate`

---

### Task 8: resumable feature shardとreference pool

**Files:**

- Create: `pipeline/references.py`
- Create: `tests/test_reference_cache.py`
- Refactor: `real_world_inference.py`
- Create: `utils/reference_selection.py`

**Tests first:** TC-22～TC-27。

**Required implementation sequence:**

1. `real_world_inference.py`のmonolithを`extract_video_frames(...)`、`select_references(...)`、`restore_frames(...)`へ先に分割する。この時点では挙動を変えない。
2. 既存prompt ensemble、CLIP normalize、Otsu clean scoreをpure helperへ移す。`resolve_reference_names`と空pool fallbackはshort modeに残す。
3. 既存short inference testを実行し、1～2のrefactor後も変わらずGREENであることを確認する。GREENになるまでlong algorithmを追加しない。
4. `frame_bytes_to_pil_rgb`を実装する。FFmpeg rawvideo `rgb24`を`uint8[height,width,3]`へreshapeし、PIL RGBへ変換して既存`clip_preprocess`へ渡す。BGR変換は禁止する。
5. preprocessed videoを順番にdecodeし、featureを10,000frame shardへ保存する。decodeはFFmpeg rawvideo pipeを使い、`trim=start_frame=S:end_frame=E,setpts=PTS-STARTPTS`、`-pix_fmt rgb24`で各frameを`width * height * 3` byteずつ読む。OpenCV seekと`CAP_PROP_FRAME_COUNT`へ依存しない。
6. shardごとにrange、shape、dtype、hash、statusをcache manifestへ保存する。途中失敗時はそのshard全体を再計算する。
7. timelineを`pool_size`個のbinへ分け、各binのclean score最小frameを選ぶ。これは既存all-pairs方式を置換するlong mode専用algorithmである。
8. pool不足時だけ全体rankingから補う。候補0件は`reference_failed`で停止し、short用fallbackを呼ばない。
9. pool frameだけ8-bit PNGへ保存する。
10. target shardごとにpoolとのcosine similarityを計算し、上位Dをmappingへ追記する。
11. mapping全体を検証してからcacheをcompleteにする。

**Memory rule:** 全target×全target similarity matrixを作らない。target shard×最大256 poolだけをGPU/CPUへ載せる。

**RED/GREEN:**

```powershell
python -m pytest tests/test_reference_cache.py tests/test_real_world_helpers.py -v
```

**Commit:** `feat: add video-wide resumable reference cache`

---

### Task 9: TAPE external reference mode

**Files:**

- Modify: `real_world_inference.py`
- Modify: `data/real_world_video_dataset.py`
- Create: `tests/test_tape_chunk.py`
- Extend: `tests/test_inference_contract.py`
- Extend: `tests/test_short_inference_contract.py`

**Tests first:** TC-28、TC-29のparser/data flow、既存short mode regression。

**Required implementation:** section 7.5のCLIとfail-closed validationを追加する。process開始直後にexternal引数が全指定か全未指定かを検査してmodeを一度だけ確定する。external modeでは`extract_video_frames`と`select_references`を呼ばず、`restore_frames`だけへ外部input/mapping/reference rootを渡す。次が0回であることをmockでassertする。

- `cv2.VideoCapture(input_video)`
- `clip.load(...)`
- Otsu threshold/reference generation

datasetはinput sequenceをchunk folder、reference imageをshared pool folderから読む。mapping key欠損、pool 0件はfallbackせずerrorにする。既存short modeだけは従来のdecode、選択、padding経路を維持する。

**Commands:**

```powershell
python -m pytest tests/test_tape_chunk.py tests/test_inference_contract.py tests/test_short_inference_contract.py -v
```

**Commit:** `feat: let TAPE consume shared external references`

---

### Task 10: chunk worker、overlap trim、FFV1 chunk

**Files:**

- Create: `pipeline/tape_chunk.py`
- Extend: `tests/test_tape_chunk.py`

**Tests first:** TC-28、TC-30、TC-31。

**Required implementation:**

- preprocessed videoからdecode rangeだけをlossless PNGへ抽出する。FFmpeg filterは`trim=start_frame=S:end_frame=E,setpts=PTS-STARTPTS`を使い、`-start_number S`でglobal indexから命名する。timestamp seekの`-ss`だけで範囲を切らない。
- filenameはglobal 9桁index。
- `--tape-worktree`をabsolute WSL pathへ変換し、section 7.6の純argvでabsolute `.venv-rocm/bin/python`とabsolute `real_world_inference.py`をexternal mode起動する。`bash -lc`は禁止する。
- restored PNGからcore rangeだけ選ぶ。
- analyze時に固定した同一Windows FFmpegでcore frameをFFV1 chunk partialへCFR encodeし、ffprobe/count/hash後にpublishする。
- chunk sidecarへWindows FFmpegのabsolute path/version、CFR指定、`avg_frame_rate`を保存する。
- metrics JSONへwall time、frame数、FPS、peak allocated/reserved、precisionを保存する。
- failure時はexit code、stderr末尾2000文字、log pathをstage recordへ保存する。

**禁止:** chunk成功前のPNG削除、reference再計算、inputへのwrite、JPEG。

**RED/GREEN:** `python -m pytest tests/test_tape_chunk.py -v`

**Commit:** `feat: add resumable TAPE chunk worker`

---

### Task 11: orchestrator resumeとGPU lock

**Files:**

- Extend: `pipeline/run_local.py`の`canary/run/resume/batch`
- Extend: `tests/test_run_local.py`

**Tests first:** TC-17～TC-21、TC-26、TC-32、TC-39。

**Required implementation:**

- manifest decisionに従ってstage/chunkをskipまたはrunする。
- process-wide GPU lockを`work/local-pipeline/.tape-gpu.lock`へ原子的に作る。
- live PID lockは待機せず説明付きで停止する。
- stale lock判定にはPID不在、host一致、process start time不一致を使う。
- batchはsourceごとに例外を隔離し、summaryを最後に書く。
- Ctrl+C時は現在stageを`interrupted`にしてmanifestをflushする。

**Fault injection:** CLIは`--fault-inject-chunk N`を持つが、環境変数`PIPELINE_ENABLE_FAULT_INJECTION=1`が無ければparser errorにする。通常helpでは`argparse.SUPPRESS`で隠す。TC-21とcanaryでのみ使用し、manifestへ`fault_injected=true`を記録する。

**Commit:** `feat: orchestrate resumable local TAPE runs`

---

### Task 12: concat、audio remux、final verification

**Files:**

- Extend: `pipeline/media_contract.py`
- Extend: `pipeline/run_local.py`の`verify`
- Extend: `tests/test_long_media_contract.py`

**Tests first:** TC-33～TC-36。

**Required implementation:**

1. chunk sidecarをindex順に読み、range連続性とcodec parameter一致を検査する。
2. concat listをrunner自身が生成する。各chunk pathをresolveし、trusted run root配下であることを確認する。CR、LF、単一quoteを含むpathとroot外pathは拒否し、user入力文字列を直接埋め込まない。
3. analyze時に固定したchunk encodeと同一のWindows FFmpegでconcat demuxerを実行し、video-only FFV1をstream copyする。
4. original sourceから全audio streamをstream copyする。
5. exact decoded video frame count、選択済み`avg_frame_rate`、audio stream数、各streamのcodecとpacket countを検証する。
6. color/SAR/DAR/fps、input hash、JPEG 0件を検証する。
7. PASS後に`E:\vhs-upscale\work\tape\<safe-stem>-<source-hash12>_tape.mkv`、隣接handoff JSON、run/chunk/reference manifestのimmutable snapshotを原子的にpublishする。run-local側はcanonical manifest、検証report、中間artifactを保持する。

**Regression:** 既存short media contract testも同時実行する。

```powershell
python -m pytest tests/test_long_media_contract.py tests/test_short_inference_contract.py -v
```

**Commit:** `feat: verify and publish lossless long-form output`

---

### Task 13: estimate、status、documentation

**Files:**

- Extend: `pipeline/run_local.py`
- Extend: `tests/test_run_local.py`
- Update: `docs/qtgmc-long-processing.md`

**Tests first:** TC-37。

**Required report:**

- source duration/frame count/rate/resolution
- selected QTGMC preset/rate
- measured QTGMC FPS
- measured TAPE FPSと測定解像度
- estimated TAPE wall time。異解像度では`rough_extrapolation`表示
- feature cache、PNG peak、FFV1、logを含むdisk estimate
- 現在のfree diskと安全margin 20%
- completed/failed/pending chunk数
- ETAはcompleted chunkの移動平均から計算し、未実測時は出さない

**Commit:** `docs: add long-run estimates and operator guide`

---

### Task 14: 30～60秒canary

**Files outside Git:**

- `work/local-pipeline/<run>/...`
- `work/logs/qtgmc-long-processing/...`

**Preconditions:**

- 実29.97i VHS sourceがある。
- field orderが自動確定またはuser override済み。
- preview selectionがvalid。
- 704×396相当以下、または新解像度のGPU smokeが先にPASS。

**Steps:**

1. 30～60秒rangeを2個以上のTAPE chunkになる短いcore sizeで実行する。
2. 2番目chunkを故障注入で1回失敗させる。
3. resumeし、2番目だけ再実行されたことをhash/mtimeで確認する。
4. final contract、input不変、audio packet、JPEG 0を確認する。
5. visual seam、flicker、over-denoise、顔/模様変形を人が確認する。
6. 結果と実測値をdocsへ記録する。

**Acceptance:** TC-40 PASS。

**Commit:** `docs: record long-pipeline canary evidence`

---

### Task 15: fresh full suiteと独立review

**Steps:**

1. Windows unitをfresh実行。
2. Windows VapourSynthをfresh実行。
3. FFmpeg integrationをfresh実行。
4. WSL GPUをfresh実行。
5. canary e2eをfresh実行。
6. `git diff <baseline>...HEAD --stat`とfile-specific diffをreviewする。
7. 独立reviewerへspec、plan、test cases、baseline、実機log、manifestを渡す。
8. Critical/Important指摘をすべて解消し、影響testとfull suiteを再実行する。

**Commands:** `@./test_cases.md` section 10を正本とする。

**Commit:** `test: finalize QTGMC long-run acceptance`

---

### Task 16: VHS一本を処理してhandoffを固定する

**Preconditions:** Task 15完了、canaryと同じsignature、十分なdisk/runtime見積り。

**Steps:**

1. `run`で一本を開始する。
2. 中断した場合は設定を変えず`resume`する。
3. 全chunk complete後、concat/remux/verifyを実行する。
4. TC-41を検証する。
5. `handoff.json`へModal入力契約を固定する。
6. docsへ実測総時間、平均TAPE FPS、peak VRAM、disk peak、失敗/retry数を記録する。

**完了条件:** `work/tape/<safe-stem>-<source-hash12>_tape.mkv`、隣接handoff JSON、run/chunk/reference manifest snapshotが全てhash一致し、元動画が不変。

## 11. TDD実行規則

各Taskで次の順序を変えない。

1. 対応TCだけをtest codeへ写す。
2. 対象testを実行し、期待理由でFAILすることを記録する。
3. 最小実装を行う。
4. 対象testをGREENにする。
5. 直接影響する既存testを実行する。
6. `git diff --check`を実行する。
7. Task単位でcommitする。

失敗理由が想定と違う場合は実装を続けず、root causeを確定する。testを弱めてGREENにしてはならない。

## 12. error分類

reportの`error.category`は次のenum以外を使わない。

```text
invalid_input
missing_dependency
media_contract
insufficient_disk
manifest_corrupt
artifact_corrupt
qtgmc_failed
reference_failed
gpu_busy
gpu_oom
tape_failed
concat_failed
audio_failed
verification_failed
interrupted
internal_error
```

各errorは`message`、`command`のredacted argument list、`exit_code`、`log_path`、`retryable`を持つ。checkpointやuser pathを秘密扱いで消す必要はないが、環境変数全体をdumpしない。

## 13. 実行時の停止条件

以下では自動fallbackしない。

- sourceがprogressive、field order不明、色/SAR metadata不明
- VapourSynth/QTGMC plugin不足
- preview selection未確定
- source、checkpoint、CLIP、Swin asset hash変化
- manifest schema不明またはJSON破損
- free diskがestimate + 20% margin未満
- overlapがtemporal window未満
- reference mapping欠損/破損
- live GPU lockあり
- GPU OOM
- chunk frame count/rate/color mismatch
- audio packet count mismatch
- input hash/size/mtime変化

GPU OOM時はpatch sizeや解像度を自動変更しない。別設定は別signature・別preview/canaryとして扱う。

## 14. リスクと軽減策

| リスク | 軽減策 |
|---|---|
| VapourSynth Python packageはあるがnative pluginがない | first-frame requestを含むpreflight、pinned extras、main runでauto-installしない |
| field order誤り | `tt/bb`以外fail closed、明示overrideとprovenance |
| QTGMC presetの画質差 | 3 preset×2 rateのpreview matrixとhuman selection gate |
| TAPEが遅い | 実測FPS、30～60秒canary、full run前estimate |
| VRAM OOM | 704×396 baselineを記録、解像度変更ごとGPU smoke、no auto fallback |
| reference cache巨大化 | 10k shard、256 pool、target shard×pool計算、resume |
| shard途中で失敗 | v1はshard単位で再計算し、shard内部resumeは将来課題として明記 |
| chunk seam | shared reference pool、global frame index、window overlap、core trim、canary visual check |
| 再実行で成果物混在 | stage signature、hash、invalid artifact隔離、atomic publish |
| audio切れ・複数stream欠落 | long/short preflightを分離し、全streamのcodec/packet数を個別比較 |
| 10-bitと信号精度の混同 | CLIP用rgb24、pool/TAPE PNGの8-bit境界をreport/handoffへ明記 |
| Windows/WSL path不一致 | 1つのpath converter、E drive以外fail closed、純argvで起動 |
| source名によるpath/concat注入 | ASCII safe stem、常時hash suffix、trusted root検査、CR/LF/quote拒否 |

## 15. 成功条件

1. Windows VapourSynth preflightが実frame requestまでPASSする。
2. 29.97i sourceからframe数2倍の59.94p progressive FFV1を生成できる。
3. 59.94pと決定論的29.97pの6候補を比較し、選択をhash付きで固定できる。
4. source全体のCLIP feature/reference poolを一度だけ計算し、全chunkが同じcache keyを使う。
5. chunk core rangeにgap/duplicateがなく、overlapが最終動画へ混入しない。
6. 失敗chunkだけをresumeし、成功chunkを変更しない。
7. final MKVがFFV1 `yuv422p10le`、選択fps、検証済み色/SAR/DARを持つ。
8. original audio全streamのcodec/packet数を保つ。
9. original source、preprocessed正式artifact、assetを変更しない。
10. JPEG 0件。
11. unit、vapoursynth、integration、gpu、e2eの全groupがfresh PASSする。
12. 一本全体のartifactが`work/tape/<safe-stem>-<source-hash12>_tape.mkv`へpublishされ、隣接handoff JSON、run/chunk/reference manifest snapshotとhashで結ばれる。

## 16. 実装者向け最終チェックリスト

- [ ] 変更開始前にTask番号と対応TC番号を確認した。
- [ ] testを先に書き、期待理由のREDを記録した。
- [ ] shell文字列、`bash -lc`、`shell=True`や`-y`による既存正式artifact上書きを追加していない。
- [ ] WSL commandはabsolute pathを1要素ずつ渡す純argvである。
- [ ] source stemを正規化し、run root外またはCR/LF/quoteを含むconcat pathを拒否した。
- [ ] source/asset/checkpointへwriteしていない。
- [ ] float fpsを使っていない。
- [ ] local chunk番号でglobal frame名を置換していない。
- [ ] chunk内でCLIP/referenceを再計算していない。
- [ ] FFmpeg `rgb24`をPIL RGBへ変換し、BGR channel swapを入れていない。
- [ ] long pipelineは全audio streamを保持し、short runnerの単一audio契約を変えていない。
- [ ] completed判定時にhashとcontractを再検証した。
- [ ] failure diagnosticを成功cleanupより先に削除していない。
- [ ] 既存short inference pathの回帰testを実行した。
- [ ] 変更後のfresh relevant test結果を記録した。
- [ ] Task単位のcommitを作成した。

## 17. 未解決事項

計画を止める設計上の未解決事項はない。実行時に必要な次の2項目はgateとして扱う。

1. 実29.97i VHS sourceの用意とfield order確定。
2. previewを見たユーザーによるpresetとTAPE入力rateの選択。

これらを実装者が推測で埋めてはならない。
