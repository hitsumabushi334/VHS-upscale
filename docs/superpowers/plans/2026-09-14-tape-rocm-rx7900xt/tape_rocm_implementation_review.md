# TAPE ROCm RX 7900 XT 実装計画レビュー

## 前提

本レビューは、以下の2ファイルを対象に実施した。

- `implementation_plan.md`
- `test_cases.md`

また、**QTGMCは現在の実装対象内へ修正済み**という前提を反映する。  
そのため、以前の「Task 8でdeinterlace済み入力を別途用意すべき」という指摘は、そのままでは適用しない。代わりに、QTGMC出力からTAPE入力までの契約を明示することを確認事項とする。

---

# 総合評価

全体として、実装計画はかなり良く整理されている。

特に以下は妥当。

- ROCm / PyTorch / TAPE model / asset / video I/O の境界を段階的に分離して検証する
- `strict=True` でcheckpoint整合性を確認する
- JPEG intermediateを禁止してPNGを使う
- 元動画を変更しない
- assetのURL・size・SHA-256を保存する
- 短尺実素材で成功してから長時間処理へ進む
- RealBasicVSR / ModalをTAPEローカル検証から分離する
- 実機ログを残して次段階の設計値として使う
- worktree上でupstreamへの変更を隔離する
- Stage失敗時に無関係な層を変更しない停止条件を設けている

設計を作り直す必要はない。

ただし、実装開始前またはTask 6以前に修正した方がよい点がある。

---

# Critical / Important

## 1. TAPEのforward I/O仕様を実コードから確定してからsmoke testを固定する

### 問題

現行計画ではTask 6において概ね以下の形を想定している。

```python
model = SwinUNet()
inputs = torch.zeros((1, 5, 3, 64, 64), device="cuda")
refs = torch.zeros((1, 5, 3, 64, 64), device="cuda")

output = model(inputs, refs)

assert output.shape == inputs.shape
```

テストケース側でも出力shapeを、

```text
[1, 5, 3, 64, 64]
```

として固定している。

しかし、この仕様がTAPE upstreamの実際の

- `SwinUNet` constructor
- `forward()` signature
- input tensor layout
- reference tensor layout
- return value
- output shape
- checkpoint state_dict構造

と一致するかは、計画書だけでは確定していない。

### リスク

TAPEが例えば、

```text
5 neighboring frames
+
5 reference frames
↓
center frame 1枚を復元
```

というモデルなら、

```text
[B, 5, 3, H, W]
```

を出力shapeとして期待するテスト自体が誤りになる。

また、実モデルがconstructor引数を必要とする場合、

```python
SwinUNet()
```

だけで正しくcheckpointをロードできない可能性もある。

### 修正要求

Task 6の最初に、upstreamコードから以下を抽出するStepを追加する。

```text
- model constructor
- constructor arguments
- checkpoint内のstate_dict形式
- forward arguments
- tensor dimension order
- output tensor shape
- normalization / range
```

その情報を根拠としてTC-10 / TC-11および`smoke_checkpoint.py`を確定する。

**推測したtensor shapeをテスト仕様へ固定しないこと。**

---

## 2. QTGMC → TAPE の入力契約を明文化する

QTGMCは実装対象内へ変更済みなので、以前の「Task 8入力を別途progressive clipにする」という指摘は不要。

代わりに、QTGMC stageとTAPE stageの境界を明確にする必要がある。

最低限、TAPE入力について以下を固定する。

```text
scan:
progressive

frame rate:
59.94p または比較対象の29.97p

CFR/VFR:
CFR

pixel / color metadata:
明示的に保存

SAR/DAR:
明示的に保存

audio:
TAPE inferenceには渡さず別streamとして保持可能
```

特に今回の目的では、

```text
29.97i
↓
QTGMC
↓
59.94p
↓
TAPE
```

と、

```text
29.97i
↓
QTGMC
↓
29.97p
↓
TAPE
```

の比較が重要。

29.97pで品質差が小さければ、TAPE側の処理frame数をほぼ半減できる可能性がある。

したがってQTGMC統合後は、**TAPE後ではなくTAPE入力前にも59.94p / 29.97p比較を置くこと。**

---

## 3. FFmpegのmedia contractを「比較」だけでなく「再構築」にする

### 問題

TC-13では以下を検査対象としている。

```text
pix_fmt
color_range
color_space
color_primaries
color_transfer
SAR
DAR
field order
time base
frame rate
audio stream数
```

これは良い。

一方、現行のFFV1 encode例は概ね、

```bash
ffmpeg \
  -framerate ... \
  -i restored/%08d.png \
  -c:v ffv1 \
  -level 3 \
  -g 1 \
  -slicecrc 1 \
  -pix_fmt yuv422p10le \
  video_only.mkv
```

となっており、色・アスペクト情報を明示的に復元していない。

### リスク

処理経路では、

```text
VHS YUV
↓
QTGMC
↓
RGB/BGR image
↓
PNG
↓
TAPE
↓
PNG
↓
YUV FFV1
```

という色変換を通る。

このとき、

```text
color_range
colorspace
primaries
transfer
SAR
DAR
```

が意図せず失われる可能性がある。

### 修正要求

入力解析時に最低限以下をmanifest/reportへ保存する。

```text
pix_fmt
color_range
color_space
color_primaries
color_transfer
sample_aspect_ratio
display_aspect_ratio
field_order
avg_frame_rate
r_frame_rate
time_base
```

最終FFmpeg encodeでは、必要なmetadataを明示的に設定する。

またTC-13は、

```text
保持すべき項目
```

と

```text
処理によって意図的に変化する項目
```

に分ける。

例：

```text
field_order:
interlaced → progressive
```

はmetadata消失ではなく、QTGMCによる意図的な変更である。

したがって単純なinput/output完全一致テストにはしない。

---

# Important

## 4. TC-03を実際のunit testへ追加する

`test_cases.md`には以下が定義されている。

```text
clean candidate: 2件
reference数: 5件

期待:
上位2件
+
先頭referenceを3回padding
```

しかしimplementation planに掲載されているTask 2の具体的テストでは、

- index再対応
- candidate空
- portrait / landscape resize

が中心で、通常のreference不足paddingケースが明示されていない。

以下相当を追加する。

```python
def test_reference_shortage_is_padded_to_requested_count():
    ...
```

期待値はTC-03と一致させる。

---

## 5. TC-05の後半もテストする

TC-05は、

```text
pytorch_lightningなし
→ dataset import成功

init_logger()呼出
→ 説明付きdependency error
```

まで要求している。

しかし現行の具体的テスト例では、

```python
importlib.import_module("data.real_world_video_dataset")
```

の成功しか確認していない。

したがって、

```python
def test_init_logger_reports_missing_lightning_dependency(...):
    ...
```

相当を追加する。

単なる`ModuleNotFoundError`ではなく、

```text
logging/training機能を使う場合のみpytorch_lightningが必要
```

と原因が分かるエラーを期待する。

---

## 6. pytestをunit / GPU / integrationに分ける

現行Task 9では、

```bash
python -m pytest -v
```

に対して、

```text
全test PASS
skipなし
GPU実機testはmarkerで実行
```

という条件がある。

これはやや曖昧。

GPU testをmarker化するのであれば、最低限以下のように分類した方がよい。

```text
unit
gpu
integration
```

例：

```bash
pytest -m "not gpu and not integration"
pytest -m gpu
pytest -m integration
```

最終acceptanceでは3セット全部を実行する。

これにより、

```text
pure unit test
ROCm実機 test
実asset / FFmpegを含むintegration test
```

の失敗原因を分離できる。

---

# Medium

## 7. ROCm setup testが文字列存在確認中心なのは弱い

現在の静的テスト案では、

```python
assert "librocdxg" in text
assert "libhsa-runtime64.so.1.2" in text
```

のような検査が含まれている。

これはscriptに文字列が存在するだけでもPASSする。

例えばbranch条件が逆でも検出できない。

可能であればruntime判定部分をpure functionまたは小さなhelperへ分離する。

例：

```python
select_runtime(rocdxg_detected=True)
# -> "rocdxg"

select_runtime(rocdxg_detected=False)
# -> "legacy"
```

またはshell scriptをdry-run modeで実行し、

```text
ROCDXG検出
→ legacy HSA runtime置換を実行しない

ROCDXGなし
→ legacy処理を選択
```

まで検証する。

---

## 8. PNGのbit depth / value rangeを確認する

JPEG排除は正しい。

ただし「PNGだから完全」とは限らない。

TAPE/OpenCVの処理が8-bit RGB/BGR前提なら、元のFFV1 `yuv422p10le`から一度8-bitへ落ちる可能性がある。

これはTAPEモデル自体が8-bit想定なら必ずしも問題ではないが、**どこで量子化されるかを明示するべき**。

確認項目：

```text
QTGMC出力bit depth
PNG保存時bit depth
OpenCV decode dtype
TAPE input dtype / normalization
TAPE output dtype / range
FFV1再encode時bit depth
```

特に、

```text
10-bit FFV1
↓
8-bit PNG
↓
10-bit FFV1
```

なら、最終ファイルが10-bitでも実データ精度は増えない。

この点は「lossless codec」と「lossless signal path」を分けて扱う。

---

# 良い点

## ROCm切り分け

以下の分離は非常に良い。

```text
Windows driver
/dev/dxg
ROCm runtime
PyTorch wheel
TAPE model
```

Stage 1失敗時にTAPE sourceを変更しない停止条件も適切。

---

## `torch.cuda`をROCmでも維持

ROCm版PyTorchでも通常は、

```python
torch.cuda
device="cuda"
```

を使用する。

したがってCUDAという名前だけを理由に機械的にHIP APIへ書き換えない方針は正しい。

---

## checkpoint strict load

```python
load_state_dict(..., strict=True)
```

で、

```text
missing key = 0
unexpected key = 0
```

を成功条件にする設計は妥当。

最初から`strict=False`へ逃げないことは重要。

---

## asset provenance

以下を残す設計は非常に良い。

```text
official source
URL
byte size
SHA-256
download time
```

checkpoint、CLIP、Swin-Tを再現可能にする上で有効。

---

## AMP / FP32の分離

```text
AMP failure
!=
model failure
```

としてFP32 baselineを独立させる設計も妥当。

RX 7900 XT / ROCmでprecision固有問題が出てもモデル全体を失敗扱いしない。

---

## 非破壊性

入力について、

```text
size
mtime
SHA-256
```

を前後比較するTC-14は十分強い。

入力動画をread-only相当として扱う今回の要件と合っている。

---

## lossless intermediate

JPEGを禁止しPNGへ変更した点は重要。

さらに、

```text
TAPE output
↓
FFV1 Matroska
```

とすることで、Modal RealBasicVSRまで不要な再圧縮を挟まない設計になっている。

---

# 修正優先順位

## 実装開始前に必須

1. **TAPE upstreamからforward signature / constructor / tensor shape / checkpoint構造を確定**
2. **QTGMC → TAPEのmedia contractを明文化**
3. **色空間・SAR/DAR等をFFmpegで再構築する仕様を追加**

## Task 2～3で追加

4. **TC-03 padding test**
5. **TC-05 logger dependency test**

## 最終検証までに修正

6. **pytestをunit / GPU / integrationに分離**

## 品質向上

7. ROCm runtime選択testを文字列検査からbehavior testへ強化
8. PNG / model I/Oのbit depthとvalue rangeを記録

---

# 判定

| 項目 | 評価 |
|---|---|
| 全体architecture | A |
| ROCm問題の切り分け | A |
| TDD設計 | A- |
| checkpoint検証 | A |
| asset再現性 | A |
| 非破壊性 | A |
| PNG / lossless方針 | A |
| TAPE model I/O定義 | 要修正 |
| QTGMC → TAPE契約 | 要明確化 |
| color / media contract | 要修正 |
| test分類 | 改善推奨 |

## 最終結論

実装計画の方向性はそのままでよく、全面的な再設計は不要。

ただし最も重要なのは、**TAPEの実際のモデルI/Oをupstreamコードで確認する前に、smoke testのtensor shapeを仕様として固定しないこと**。

またQTGMCが対象内へ変更されたため、今後は、

```text
VHS
↓
QTGMC
↓
59.94p / 29.97p
↓
TAPE
```

を一つの連続したローカルpipelineとして設計し、QTGMC出力のframe rate、progressive状態、色metadata、SAR/DARをTAPE入力契約として明文化する。

この点を修正すれば、実装へ進める品質に達している。
