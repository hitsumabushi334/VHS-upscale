# TAPE ROCm RX 7900 XT — Task 4完了後の引継書

作成日: 2026-09-17

## 1. 現在地

- Task 1（隔離worktree）とTask 4（ROCm環境構築script・Stage 1 checker）は完了。
- Task 4受入れcommit: `3d8fe192e6b01a44d7602c3cbcf9d24bbbcdb231`
- Worktree: `E:\vhs-upscale\worktrees\tape-rocm-rx7900xt-luna`
- Branch: `codex/tape-rocm-rx7900xt-luna`
- Upstream baseline: `fb97c608a1733dec5dbcd5aeb77ed84e29b793f7`
- 未実行順序: **Task 5 -> Task 2 -> Task 3 -> Task 6 -> Task 7 -> Task 8 -> Task 9**
- Task 5の実機ROCmセットアップはまだ実行していない。本セッションはTask 4受入れと本書作成で停止した。

旧branch `codex/tape-rocm-rx7900xt` とcommit `65289fa` は、モデル監査上の不適合があるため監査資料としてのみ残し、実装へ取り込まないこと。

## 2. Task 4の受入れ根拠

- 独立read-onlyレビュー: Critical 0 / Important 0 / Minor 0、全既知指摘解消、Task 4 accepted。
- 最終テスト: `77 passed in 26.66s`、skip 0。
- `bash -n scripts/wsl/setup_rocm_env.sh`: pass。
- `scripts/check_rocm.py`と`tests/test_rocm_scripts.py`のPython構文: pass。
- `git diff --check`: pass。
- 最終commitまでの実装修正はすべてfresh `gpt-5.6-luna / xhigh` の一回限りの担当で、各session JSONLの全`turn_context`を監査済み。
- 最初の最終テスト試行はsandbox内の全WSL起動が`Wsl/Service/CreateInstance/E_ACCESSDENIED`になり69件が連鎖失敗した。これは製品コードの失敗ではない。WSL実行権限付きの再試行で77/77合格した。

主な成果物:

- `requirements-rocm-inference.txt`
- `scripts/wsl/setup_rocm_env.sh`
- `scripts/check_rocm.py`
- `tests/test_rocm_scripts.py`
- `pytest.ini`
- `.superpowers/sdd/implementation_plan/task-4-report.md`
- `.superpowers/sdd/implementation_plan/task-4-acceptance-review-9.md`

## 3. モデル運用の必須条件

- 実装・修正は毎回、**新規spawnした `gpt-5.6-luna` / `xhigh`**へ、明確に限定した一つのturnとして委任する。
- 実装agentをresumeしない。追加入力を送らない。過去にresume後のturnがcoordinatorの`gpt-5.6-sol / medium`を継承したためである。
- commitを受け入れる前に、そのagentのsession JSONLにある全`turn_context`を検査し、modelがLuna以外またはeffortがxhigh以外なら即不採用にする。
- 独立レビューは`gpt-5.6-sol / high`をread-onlyで使用可。ただし編集・commit・実装は禁止。
- 各feature/fixはTDD、失敗時は`superpowers:systematic-debugging`、完了宣言前はfresh verification、意味のある変更後は独立レビューを維持する。

## 4. 固定済み方針

- 対象はこのPCのRX 7900 XT。
- 現在機の実行経路は明示的な`legacy` ROCm 7.2。
- `rocdxg`は承認済みimmutable artifact allowlistがないため通常setupを必ずfail closedする。legacyへauto-fallbackせず、legacy userspace/wheel/HSA置換actionも実行しない。
- 既存`.venv-rocm`を暗黙再利用・再ラベルしない。runtime identityの矛盾は拒否する。
- QTGMCと長時間処理は最終的な対象。ただし現implementation planは短尺TAPE実証までで、完了後に別計画へ渡す。
- RealBasicVSRはローカルTAPEから分離し、Modal上の別spec・別implementation planで構築する。

## 5. 次に実行するTask 5

必ず最新worktree `E:\vhs-upscale\worktrees\tape-rocm-rx7900xt-luna` を使う。元plan記載の旧worktree名へ戻さない。

1. host preflight:

```powershell
wsl.exe -d Ubuntu-22.04 -- bash -lc "ls -l /dev/dxg; python3 --version; uname -r; df -h /mnt/e"
```

期待値: `/dev/dxg`あり、Python 3.10、十分な空き容量。

2. log directoryを用意し、legacy setupを実行:

```powershell
wsl.exe -d Ubuntu-22.04 -- bash -lc "mkdir -p /mnt/e/vhs-upscale/work/logs/tape-rocm && cd /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna && bash scripts/wsl/setup_rocm_env.sh --runtime legacy 2>&1 | tee /mnt/e/vhs-upscale/work/logs/tape-rocm/stage1-setup.log"
```

3. GPU smoke test:

```powershell
wsl.exe -d Ubuntu-22.04 -- bash -lc "cd /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna && .venv-rocm/bin/python scripts/check_rocm.py --json /mnt/e/vhs-upscale/work/logs/tape-rocm/stage1-environment.json"
```

期待値: RX 7900 XT、HIP non-null、FP32 finite。FP16結果も記録する。

4. 実測versionを`docs/rocm-rx7900xt.md`へ記録し、レビュー・検証後にcommitする。

Task 5停止条件: Stage 1失敗時はTask 2以降へ進まず、systematic debuggingでdriver、`/dev/dxg`、runtime、wheelの境界を特定する。ROCDXGやsynthetic successへ逃がさない。

## 6. 残タスクの順序とゲート

1. Task 5: RX 7900 XTでStage 1実機証拠を作成。
2. Task 2: reference選択とresizeの回帰修正。
3. Task 3: PNG既定値と推論import軽量化。
4. Task 6: asset provenance、checkpoint契約、smoke test。
5. Task 7: 実assetとRX 7900 XTでStage 2/3を実行。
6. Task 8: 実VHS短尺でPNG・FFV1・音声保持推論。
7. Task 9: 最終検証、独立レビュー、QTGMC・長時間処理・Modal計画への引渡し。

Task 8の絶対ゲート:

- `E:\vhs-upscale\input\VHS_10s.mkv` が必要。
- progressive/CFR化済みの実VHS 5～10秒sampleであることをffprobeで確認する。
- 欠損時は停止する。synthetic fixtureで実素材成功を代用しない。

## 7. 参照資料

- Plan: `E:\vhs-upscale\docs\superpowers\plans\2026-09-14-tape-rocm-rx7900xt\implementation_plan.md`
- Design: `E:\vhs-upscale\docs\superpowers\specs\2026-09-14-rx7900xt-tape-rocm-design.md`
- Test cases: `E:\vhs-upscale\docs\superpowers\plans\2026-09-14-tape-rocm-rx7900xt\test_cases.md`
- Progress: `E:\vhs-upscale\worktrees\tape-rocm-rx7900xt-luna\.superpowers\sdd\implementation_plan\progress.md`
- Task 4 report: `E:\vhs-upscale\worktrees\tape-rocm-rx7900xt-luna\.superpowers\sdd\implementation_plan\task-4-report.md`
- Final review: `E:\vhs-upscale\worktrees\tape-rocm-rx7900xt-luna\.superpowers\sdd\implementation_plan\task-4-acceptance-review-9.md`
- Stage 1 logs: `E:\vhs-upscale\work\logs\tape-rocm\`

作業開始時にbranch/HEAD/statusを確認し、ユーザーの既存変更を保持すること。Task 5以降も各Phase完了時に、accepted commit、モデル監査、検証結果、artifact/log、未解決risk、次Taskの正確なcommandと停止条件をまとめた次session用引継書を作成する。
