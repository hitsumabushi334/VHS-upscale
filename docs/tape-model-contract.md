# TAPE Model Contract (upstream commit fb97c608a1733dec5dbcd5aeb77ed84e29b793f7)

Source facts pinned with line numbers for the inference path used by
`real_world_inference.py` and `scripts/smoke_checkpoint.py`.

## Constructor

`models/swin_unet.py:35-48`: `SwinUNet()` takes no required arguments; every
`__init__` parameter has a default:

```
in_chans=3, embed_dim=96, depths=None, num_heads=None,
window_size=(2, 8, 8), mlp_ratio=4., qkv_bias=True, qk_scale=None,
drop_rate=0., attn_drop_rate=0., drop_path_rate=0.2,
norm_layer=nn.LayerNorm, use_checkpoint=False
```

So the inference model is constructed as bare `SwinUNet()`.

## Forward layout

`models/swin_unet.py:176-187`: `forward(imgs_lq, imgs_ref)` with
`imgs_lq: [B, T, C, H, W]`, `imgs_ref: [B, D, C, H, W]` and output
`out + restored` with the same shape as the input window `[B, T, C, H, W]`.

## Checkpoint loading (official inference)

`real_world_inference.py:148-150` (pre-Task-3 line numbers): the release
checkpoint is loaded as a raw state dict:

```
state_dict = torch.load(args.checkpoint_path, map_location="cpu")
model.load_state_dict(state_dict, strict=True)
```

Upstream `main.py:44-54` handles the training-side Lightning wrapper form:
if the checkpoint is a dict with a `"state_dict"` key holding `"net."*`
parameters, it strips the `"net."` prefix. `scripts/check_assets.py`
accepts exactly these two forms (`raw_state_dict`, `lightning_wrapper`)
and rejects anything else; the actual container of the release checkpoint is
recorded in `work/assets/assets.json` at fetch time, not guessed here.

## Frame I/O boundaries

`data/real_world_video_dataset.py:62-86`: frames are read with OpenCV as
8-bit BGR, converted to RGB, scaled to float32 `[0, 1]`, then passed through
`ToTensor()`. Input windows shorter than `num_input_frames` are black-padded.

`real_world_inference.py:168-176` (pre-Task-3 line numbers): model output is
clamped to `[0, 1]`, multiplied by 255, converted RGB to BGR, quantized to
uint8 and written to disk.

## Smoke shapes

Per the forward contract above, the smoke test uses
`inputs: [1, 5, 3, 64, 64]`, `refs: [1, 5, 3, 64, 64]` and expects
output shape `[1, 5, 3, 64, 64]` with all-finite values.
