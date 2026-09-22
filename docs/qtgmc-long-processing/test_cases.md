# QTGMC・長時間ローカル処理 テストケース

## 0. この文書の使い方

- 実装計画書 `@./implementation_plan.md` の各Taskは、ここにあるTC番号を先にテスト化する。
- test名は `test_tcXX_...` で始め、失敗時にTC番号がpytest出力へ残るようにする。
- unit testはGPU、実動画、network、インストール済みVapourSynth pluginへ依存させない。
- 外部commandはargument listを検査するfake runnerへ差し替え、shell文字列の一致テストにしない。
- 実機testはfixture不足を成功扱いのskipにしない。明示的なmarker選択時に前提が無ければFAILする。

## 1. 実行グループ

- `unit`: 純粋Python、tmp_path、fake subprocess、stub frame processorだけで実行する。
- `vapoursynth`: Windows Python、VapourSynth、vsjetpack、native plugin、vspipe、ffmpegを要求する。
- `gpu`: WSL2、ROCm、RX 7900 XT、TAPE checkpoint、CLIP RN50x4、Swin-Tを要求する。
- `integration`: FFmpeg/ffprobeと短いfixtureを要求する。GPUは要求しない。
- `e2e`: 30～60秒の実interlaced VHS素材と実GPUを要求する。
- 最終受入では5群を別々に実行し、選択群内の予期しないskipを0件にする。

## 2. 固定用語

- frame rangeは0始まり、終端を含まないhalf-open interval `[start, end)` とする。
- `core range`: 最終成果物へ残すframe範囲。
- `decode range`: overlap込みでTAPEへ渡すframe範囲。
- `overlap`: TAPEのtemporal windowを満たす前後frame。`num_input_frames=5`なら最小値は2。
- `source fingerprint`: source SHA-256、size、ffprobe contractのcanonical JSON SHA-256。
- `stage signature`: stage名、source fingerprint、当該stage設定、tool version、直接依存artifact hashのcanonical JSON SHA-256。
- canonical JSON: UTF-8、key sort、余分な空白なし、末尾改行なし。

## 3. VapourSynth・QTGMC

### TC-01: VapourSynth環境preflight

- Windows上でVapourSynth version、Python version、vspipe path、ffmpeg path、plugin inventoryを収集する。
- `vsdeinterlace.QTempGaussMC`をimportし、synthetic interlaced clipの先頭1frameを実際にrequestする。
- 期待結果: importだけでなくlazy plugin呼出しまで成功する。
- plugin不足時の期待結果: `status=missing_dependency`、不足したnamespace/function、plugin inventory、復旧commandをreportへ残し、exit code 1。
- 禁止結果: import成功だけでpreflight成功にする。

### TC-02: 入力field orderのfail-closed判定

- `field_order=tt`は自動でTFFとして受理する。
- `field_order=bb`は自動でBFFとして受理する。
- `field_order=tb`、`bt`、`unknown`、欠損、`progressive`はQTGMC対象として自動受理しない。
- `tb`、`bt`、`unknown`は明示的な`--field-order tff|bff` overrideがある場合だけ受理する。
- `progressive`はoverrideがあっても通常のdeinterlaceでは拒否する。
- 期待結果: source値とoverride値を両方manifestへ記録する。

### TC-03: 色・SAR metadataのfail-closed判定

- `color_range`、`color_space`、`color_primaries`、`color_transfer`、SAR、DAR、frame rateが揃ったfixtureは受理する。
- いずれかが欠損した入力は拒否する。
- 明示的なmedia override JSONがある場合だけ補完を許可し、各fieldのprovenanceを`source`または`user_override`として記録する。
- BT.601、BT.709、SAR値をファイル名や解像度から推測しない。

### TC-04: QTGMC bobのframe数とrate

- 入力: 300 frames、`30000/1001` interlaced、TFF synthetic clip。
- `fps_mode=double`の期待結果: 600 frames、`60000/1001` progressive。
- width、height、SARは入力と同一。
- 出力frame property `_FieldBased`はprogressive相当。

### TC-05: 29.97p化の決定性

- TC-04の600frame clipを`std.SelectEvery(cycle=2, offsets=0)`相当で処理する。
- 期待結果: 300 frames、`30000/1001`、選択source indexは`0,2,4,...,598`。
- 同一入力・同一設定を2回実行し、frame hash列が一致する。
- 禁止結果:平均化、補間、可変frame rate化。

### TC-06: preset registry

- registryには`raw`、`light`、`strong`の3件だけを置く。
- 各presetはQTempGaussMCへ渡す完全なparameter mappingを返す。
- mappingは呼出し側から変更できない。
- 未知presetは実処理開始前に拒否する。
- reportにはpreset名だけでなく展開済みparameter mappingも保存する。

### TC-07: QTGMC出力の原子的publish

- vspipe/ffmpegは`<name>.partial.mkv`へ書く。
- command失敗時、0 byte、contract不一致時は正式pathへrenameしない。
- 成功時だけ同一volume上の`os.replace`で`<name>.mkv`へpublishする。
- 既存のvalid artifactは上書きしない。
- stale `.partial.mkv`はmanifestへ`interrupted`として記録し、正式成果物として扱わない。

### TC-08: QTGMC media contract

- 出力videoはFFV1、`yuv422p10le`、progressive。
- `fps_mode=double`では入力frame rateの正確な2倍。
- 色属性、SAR、DARは明示的にencodeされる。
- original audioはstream copyされ、stream数、codec、全streamのpacket数が一致する。
- 2 audio streamのfixtureをlong preflightへ渡すと受理し、既存short preflightへ渡すと従来どおり拒否する。
- input size、mtime_ns、SHA-256は実行前後で一致する。

### TC-09: preview matrix

- 1つの代表rangeに対し、3 preset × 2 TAPE入力rateで6候補を生成する。
- candidate keyは`{preset}-{60000_1001|30000_1001}`のように一意で安定する。
- 各候補にinput/output hash、frame count、fps、QTGMC wall time、TAPE wall time、TAPE FPS、peak VRAMを記録する。
- 6件のうち一部失敗時、成功候補を削除せずmatrix全体を`incomplete`とする。

### TC-10: preview選択gate

- `selection.json`にはpreview matrix hash、preset、TAPE入力rate、選択日時、任意noteを必須とする。
- matrix hash不一致、未知candidate、失敗candidateの選択は拒否する。
- valid selectionなしで30～60秒canaryまたはfull runへ進めない。

## 4. Chunk計算

### TC-11: core/decode rangeの基本例

- 入力: `total_frames=100`、`core_frames=30`、`overlap=2`。
- 期待結果:
  - chunk 0: core `[0,30)`, decode `[0,32)`, trim left/right `0/2`
  - chunk 1: core `[30,60)`, decode `[28,62)`, trim `2/2`
  - chunk 2: core `[60,90)`, decode `[58,92)`, trim `2/2`
  - chunk 3: core `[90,100)`, decode `[88,100)`, trim `2/0`
- core rangeのunionは`[0,100)`で、重複も穴もない。

### TC-12: temporal window最小overlap

- `num_input_frames=5`では`overlap>=2`だけを受理する。
- `num_input_frames=7`では`overlap>=3`だけを受理する。
- 偶数または1未満の`num_input_frames`は拒否する。
- 自動補正せず、設定エラーとして処理開始前に停止する。

### TC-13: 短い動画と端数chunk

- `total_frames=1`、`core_frames=300`、`overlap=2`は1chunk `[0,1)`を返す。
- `total_frames=300`、`core_frames=300`は1chunkだけを返す。
- `total_frames=301`は2chunkを返し、末尾coreは`[300,301)`。
- `total_frames<=0`は拒否する。

### TC-14: 秒からframeへの変換

- frame rateはfloatへ変換せず`Fraction`で扱う。
- `30000/1001`、10秒はround-half-upで300 frames。
- `60000/1001`、10秒はround-half-upで599 frames。
- 変換結果と元の有理数文字列をmanifestへ保存する。

## 5. Manifest・resume・invalid化

### TC-15: manifestの原子的更新

- writeは同じdirectoryのtemporary fileへflush、fsync後、`os.replace`する。
- 書込途中を故障注入しても以前のvalid JSONが残る。
- schema version不明、JSON破損、必須key欠損はfail closed。

### TC-16: stage状態遷移

- 許可: `pending -> running -> complete`、`pending -> running -> failed`、`failed -> running`。
- 起動時に残った`running`は`interrupted`へ変換後、再実行対象にする。
- `complete`へはartifact存在、size>0、SHA-256一致、stage contract PASS時だけ遷移できる。

### TC-17: 同一signatureのskip

- manifestのstage signature、artifact hash、contractがすべて一致するcompleted stageだけskipする。
- skip時はsubprocessを1回も呼ばない。
- reportへ`decision=skipped_valid_cache`を残す。

### TC-18: artifact改変検出

- completed artifactの1 byteを変更する。
- 期待結果: hash不一致を検出し、そのstageと下流だけ再実行する。
- 改変artifactを削除せず`.invalid-<timestamp>`へ隔離する。

### TC-19: 設定変更のinvalid化DAG

- QTGMC preset、field order、QTGMC fps変更: `qtgmc -> references -> tape_chunks -> concat -> remux -> verify`をinvalid化。
- CLIP asset hash、pool size、reference数変更: `references`以降をinvalid化。QTGMCは保持。
- TAPE checkpoint hash、patch size、precision変更: `tape_chunks`以降をinvalid化。referencesは保持。
- chunk size、overlap変更: `tape_chunks`以降をinvalid化。
- mux設定変更: `concat/remux/verify`だけをinvalid化。
- report formattingだけの変更でmedia stageをinvalid化しない。

### TC-20: source変更

- 同じpathでもsize、SHA-256、media contractのいずれかが変われば別source fingerprintになる。
- source変更時は全stageをinvalid化する。
- mtimeだけの変更はSHA-256とsizeが同一ならwarningを残し、hashベースの同一性を優先する。

### TC-21: 失敗chunkだけの再開

- 4chunk中0、1、3がcomplete、2がfailedのmanifestを用意する。
- resume時はchunk 2だけを実行する。
- chunk 0、1、3のmtimeとhashは変わらない。
- 故障注入を使ったattemptには`fault_injected=true`が記録され、通常の失敗と区別できる。

## 6. 動画単位reference cache

### TC-22: cache key

- key材料: preprocessed video SHA-256、frame count/rate、preset/rate selection、CLIP model SHA-256、prompt set version、feature dtype、pool algorithm version、pool size、reference数。
- canonical JSONが同一なら同じkey、1項目でも違えば別key。
- absolute pathと実行日時はkeyへ含めない。
- `frame_bytes_to_pil_rgb`は長さが`width * height * 3`でないbyte列を拒否する。
- 既知の赤・緑・青pixelを含む`rgb24` byte列は`uint8[height,width,3]`、PIL mode `RGB`へ変換され、R/B channelが入れ替わらない。

### TC-23: feature shard resume

- 25 frames、shard size 10なら`000000-000010.npz`、`000010-000020.npz`、`000020-000025.npz`を作る。
- 先頭2 shard complete、末尾shard途中失敗後のresumeでは末尾だけを再計算する。
- 各shardにframe range、shape、dtype、hashを記録する。
- duplicate frame index、gap、非有限featureは拒否する。

### TC-24: deterministic pool選択

- prompt ensemble、CLIP normalize、Otsu clean scoreは既存short実装と同じ結果を返す。
- timeline binごとにclean score最小frameを1件選び、同値はframe index小を選ぶ。
- pool size不足時だけ全体clean rankingから未選択frameを補う。
- 同じ入力を2回実行してpool frame index列が一致する。
- pool sizeより候補が少ない場合は利用可能候補を使い、reference数不足分は最上位を繰り返す。
- long modeのpoolが0件なら`reference_failed`で停止し、short用empty fallbackを呼ばない。

### TC-25: reference mapping

- long modeは既存のclean candidate全件all-pairs方式を呼ばず、固定poolとの比較だけを行う。
- 各target featureからpool内cosine similarity上位D件を選ぶ。
- similarity同値はglobal frame index小を優先する。
- mapping key/valueは9桁global frame名`000000123.png`を使う。
- 全target frameにちょうどD件のreferenceが存在する。

### TC-26: 全chunkで同一cacheを使う

- 4chunkのworker requestすべてに同一reference cache key、manifest hash、reference rootを渡す。
- chunk内でCLIP modelをloadしない。
- chunkごとの`references.json`再生成を禁止する。

### TC-27: cache破損

- pool PNG、feature shard、mappingのいずれかを改変する。
- 期待結果: hash不一致のcacheを使用せず、破損範囲だけ再構築する。
- TAPE chunkを誤ったcacheで実行しない。
- feature shard途中の失敗はそのshard全体だけを再計算する。v1ではshard内frame単位のresumeを行わない。

## 7. TAPE chunk worker

### TC-28: global frame名を保つ抽出

- decode range `[28,62)`を抽出すると、入力PNG名は`000000028.png`から`000000061.png`。
- local 0始まりへrenameしない。
- ffmpegのselect/filter式はargument listで渡し、shell interpolationを使わない。
- WSL起動argvは`["wsl.exe", "-d", distro, "--", absolute_wsl_python, absolute_wsl_script, ...]`で、`bash`、`-lc`、command連結文字列を含まない。
- input、mapping、reference、outputの各absolute WSL pathは、それぞれ1つのargv elementである。

### TC-29: external reference入力

- `real_world_inference.py`へpre-extracted input root、external mapping、external reference rootを渡す。
- external modeではvideo decodeとCLIP reference selectionを実行しない。
- mapping欠損、reference PNG欠損、D件未満、入力frame名不一致はmodel load前に拒否する。

### TC-30: overlap trim

- TC-11の各chunkでrestored PNGをcore rangeへtrimする。
- chunk成果物のframe数は順に30、30、30、10。
- concat後のglobal index列は0～99が各1回、昇順。
- overlap frameが最終成果物へ重複しない。

### TC-31: worker成功publish

- workerはchunk FFV1を`.partial.mkv`へencodeする。
- frame count、fps、dimensions、pix_fmt、color fields、global range sidecarが一致した場合だけpublishする。
- chunk encodeはanalyze時に固定したWindows FFmpeg path/version/CFR指定を使い、sidecarの`avg_frame_rate`は選択済みrateと一致する。
- 成功後に限りchunkのinput/restored PNG削除を許可する。
- 失敗時はdiagnostic用PNGとstderr末尾を保持する。

### TC-32: GPU処理の直列化

- batchに複数動画・複数chunkがあってもTAPE GPU worker同時実行数は1。
- lockにはPID、host、run id、開始時刻を記録する。
- 生存PIDのlockを奪わない。stale lockは明示的なrecovery判定後だけ置換する。

## 8. concat・音声・最終契約

### TC-33: lossless concat

- 同一codec parameterの4chunkをframe順にconcatする。
- 期待結果: decoded frame count 100、rate維持、duration=`100/fps`許容差以内。
- chunk順が逆、欠損、重複、parameter不一致ならconcat前に拒否する。
- source stemが`[A-Za-z0-9][A-Za-z0-9._-]{0,79}`へ完全一致しない場合は`source-<hash12>`へ置換し、run/output名には常にhash12を含める。
- concat対象pathはresolve後にtrusted run root配下でなければ拒否する。CR、LF、単一quoteを含むpathもconcat list作成前に拒否する。

### TC-34: original audio remux

- video-only concat成果物とoriginal sourceから全audio streamを`-c copy`でremuxする。
- 2 audio streamのfixtureをlong pipelineが受理する一方、既存short runnerの単一stream制約は変わらない。
- audio streamが0本のsourceはlong preflightで処理開始前に拒否する。
- audio stream数、各codec、各packet数がsourceと一致する。
- video durationとaudio/container durationの差は設定した許容差以内。

### TC-35: final media contract

- video codec FFV1、pix_fmt `yuv422p10le`、progressive、選択済みfps。
- width/height、SAR/DAR、color range/space/primaries/transferがpreprocessed manifestと一致する。
- TAPEの8-bit PNG/OpenCV境界と10-bit FFV1 containerを同一精度と表現しないwarningがreportにある。
- warningはCLIP feature用FFmpeg `rgb24`とreference pool PNGも8-bitであることを明記する。
- JPEGは0件。

### TC-36: 非破壊性

- original sourceとpreprocessed正式artifactのsize、mtime_ns、SHA-256は下流処理前後で一致する。
- `input/`、checkpoint、asset cacheをwrite targetにしない。
- output pathがinput path配下または同一pathなら処理開始前に拒否する。

## 9. orchestration・batch・実測gate

### TC-37: runtime/disk見積り

- reportは入力frame数、選択fps、実測TAPE FPS、予測TAPE時間、QTGMC実測、PNG worst-case、FFV1空き容量marginを出す。
- TAPE FPSが未実測、または対象解像度がbenchmark解像度と異なる場合、数値を`measured`と表示しない。
- disk不足時は成果物作成前に停止する。

### TC-38: canary gate

- full run前に同じpreset、rate、chunk設定で30～60秒canaryを完了する。
- canary reportは全contract PASS、JPEG 0、input不変、reference共有、resume故障注入PASSを要求する。
- canary signatureとfull run設定が違う場合、full runを拒否する。

### TC-39: batch分離

- 2入力をbatchへ渡すと別run id、別manifest、別reference cache namespace、別output directoryを持つ。
- 1入力失敗でも他方のcompleted成果物をinvalid化しない。
- batch summaryは各runのstatusとmanifest pathを列挙する。

### TC-40: end-to-end 30～60秒実素材

- 入力: 実29.97i VHS、field order確定、色/SAR metadata確定、audioあり。
- QTGMC 59.94pまたは選択済み29.97p、共通reference cache、2個以上のTAPE chunkを通す。
- 故障注入で2番目chunkを一度失敗させ、resumeでそのchunkだけ再実行する。
- final report `status=ok`、全contract PASS、input不変。

### TC-41: VHS一本の最終受入

- TC-40と同じ設定・asset hash・tool versionで一本全体を処理する。
- 全chunk complete、gap/duplicate 0、全artifact hash一致。
- final MKVは`work/tape/<safe-stem>-<source-hash12>_tape.mkv`へpublishする。同directoryへhandoff JSONとrun/chunk/reference manifestのimmutable snapshotを隣接配置し、canonical run manifest、preview selection、実行logはrun directoryにも保持する。
- Modal引渡しmanifestにfinal path/hash、video/audio contract、frame count/rate、色/SAR/DAR、TAPE precision、source hashを記録する。

## 10. 最終実行command

Windows unit:

```powershell
python -m pytest -m "not vapoursynth and not gpu and not integration and not e2e" -v
```

Windows VapourSynth:

```powershell
python -m pytest -m vapoursynth -v
```

FFmpeg integration:

```powershell
python -m pytest -m integration -v
```

WSL GPU:

```powershell
wsl.exe -d Ubuntu-22.04 -- /mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing/.venv-rocm/bin/python -m pytest /mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing/tests --rootdir=/mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing -m gpu -v
```

30～60秒end-to-end:

```powershell
python -m pytest -m e2e -v
```
