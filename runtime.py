from dataclasses import dataclass
import json
import logging
import time

import torch
from safetensors.torch import load_file
import comfy.model_management as mm
from comfy.model_patcher import ModelPatcher
from comfy.utils import ProgressBar

from .downloads import resolve
from .model import YuE2Model
from .vae import YuE2VAE
from .tokenizer import YuE2TextTokenizer
from .protocol import SongRequest, GenerationConfig, token_prefixes, negative_prefix, CODEC_OFFSET, resolve_sampling
from .sampling import generate_tokens
from .nar import synthesize


@dataclass(frozen=True)
class Plan:
    request: SongRequest
    abc: str | None
    abc_ids: list[int]
    prefix: list[int]
    truncated: bool = False


@dataclass
class MusicModel:
    patcher: ModelPatcher
    tokenizer: YuE2TextTokenizer
    generation: GenerationConfig

    def prepare(self, tokens, branches=1):
        c = self.patcher.model.config
        kv_bytes = 2 * c.num_hidden_layers * c.num_key_value_heads * c.head_dim * tokens * 2 * branches
        logging.info("YuE2: loading music model to GPU (budget %d tokens, %d branch(es))", tokens, branches)
        mm.load_models_gpu([self.patcher], memory_required=kv_bytes + 2 * 1024**3, force_full_load=True)
        return self.patcher.model


def load_models(download_missing=True):
    device = mm.get_torch_device()
    if device.type != "cuda" or not torch.cuda.is_bf16_supported():
        raise RuntimeError("FL YuE2 currently requires an NVIDIA GPU with BF16 support.")
    logging.info("YuE2: resolving checkpoints (download_missing=%s)", download_missing)
    paths = [resolve(name, download_missing) for name in ("YuE2-3B", "YuE2-Vae")]
    logging.info("YuE2: loading YuE2-3B weights from %s", paths[0])
    config = json.loads((paths[0] / "config.json").read_text())
    with torch.device("meta"):
        model = YuE2Model(config)
    model.load_state_dict(load_file(str(paths[0] / "model.safetensors")), strict=True, assign=True)
    model.eval()
    patcher = ModelPatcher(model, device, mm.unet_offload_device())
    generation = GenerationConfig.from_dict(json.loads((paths[0] / "yue2_generation_config.json").read_text()))
    music = MusicModel(patcher, YuE2TextTokenizer(paths[0] / "qwen.tiktoken"), generation)

    logging.info("YuE2: loading YuE2-Vae decoder from %s", paths[1])
    config = json.loads((paths[1] / "config.json").read_text())
    with torch.device("meta"):
        vae = YuE2VAE(config)
    weights = load_file(str(paths[1] / "model.safetensors"))
    weights = {key.replace(".weight_g", ".parametrizations.weight.original0").replace(".weight_v", ".parametrizations.weight.original1"): value
               for key, value in weights.items() if key.startswith("decoder.")}
    vae.load_state_dict(weights, strict=True, assign=True)
    vae.eval()
    logging.info("YuE2: models ready on %s", device)
    return music, ModelPatcher(vae, device, mm.unet_offload_device())


def cancelled():
    mm.throw_exception_if_processing_interrupted()
    return False


def token_progress(total, label):
    bar = ProgressBar(total)
    count = 0
    started = time.perf_counter()
    last_log = started
    log_every = max(1, min(64, total // 20 or 1))

    def update(phase, token):
        nonlocal count, last_log
        count += 1
        bar.update_absolute(count)
        now = time.perf_counter()
        done = count >= total or (now - last_log >= 1.0 and count % log_every == 0)
        if done or count == 1:
            elapsed = max(now - started, 1e-6)
            tps = count / elapsed
            pct = 100.0 * count / max(total, 1)
            logging.info(
                "YuE2: %s [%s] token %d / %d (%.1f%%) @ %.1f tok/s",
                label, phase, count, total, pct, tps,
            )
            last_log = now
    return update


def acoustic_progress(steps, chunks=1):
    total = max(steps * max(chunks, 1), 1)
    bar = ProgressBar(total)
    started = time.perf_counter()
    last_log = started

    def update(completed, overall):
        nonlocal last_log
        bar.update_absolute(completed, max(overall, total))
        now = time.perf_counter()
        if completed == 1 or completed >= overall or now - last_log >= 1.0:
            elapsed = max(now - started, 1e-6)
            pct = 100.0 * completed / max(overall, 1)
            logging.info(
                "YuE2: acoustic flow %d / %d (%.1f%%) in %.1fs",
                completed, overall, pct, elapsed,
            )
            last_log = now
    return update


def make_plan(music, style, lyrics, seed, mode, abc, max_tokens):
    request = SongRequest(style=style, lyrics=lyrics, seed=seed, cot=mode, abc=abc.strip() or None)
    if mode == "off":
        logging.info("YuE2: compose planning=off (direct generation, no score)")
        return Plan(request, None, [], token_prefixes(request, music.tokenizer))
    if request.abc is not None:
        ids = music.tokenizer.encode(request.abc)
        logging.info("YuE2: compose using supplied ABC (%d tokens, planning=%s)", len(ids), mode)
        return Plan(request, request.abc, ids, token_prefixes(request, music.tokenizer, ids))
    logging.info("YuE2: composing ABC score (planning=%s, max_score_tokens=%d, seed=%d)", mode, max_tokens, seed)
    sampling = resolve_sampling({"max_tokens": max_tokens, "min_tokens": min(32, max_tokens)}, music.generation.abc)
    prefix = token_prefixes(request, music.tokenizer)
    model = music.prepare(len(prefix) + max_tokens)
    ids, timing, truncated = generate_tokens(
        model, prefix, sampling, seed, "abc", cancelled=cancelled,
        on_token=token_progress(max_tokens, "score"),
    )
    if truncated:
        raise ValueError("The score reached its token limit. Increase max_score_tokens, simplify the composition, or try another seed. Select planning=off only if you want direct generation.")
    logging.info(
        "YuE2: score done (%d tokens, %.1fs, %.1f tok/s)",
        timing.get("content_tokens", len(ids)), timing.get("seconds", 0.0), timing.get("output_tps", 0.0),
    )
    return Plan(request, music.tokenizer.decode(ids), ids, token_prefixes(request, music.tokenizer, ids))


def render(music, plan, max_seconds, temperature, top_p, top_k, repetition_penalty, cfg_scale, steps):
    if token_prefixes(plan.request, music.tokenizer, plan.abc_ids) != plan.prefix:
        raise ValueError("YuE2 plan changed. Submit edited ABC through the Plan node.")
    max_tokens = round(max_seconds * 25)
    logging.info(
        "YuE2: rendering semantic tokens (max_duration=%ds ~ %d tokens, guidance=%.2f, steps=%d, seed=%d)",
        max_seconds, max_tokens, cfg_scale, steps, plan.request.seed,
    )
    sampling = resolve_sampling({"max_tokens": max_tokens, "min_tokens": min(200, max_tokens),
                                 "temperature": temperature, "top_p": top_p, "top_k": top_k,
                                 "repetition_penalty": repetition_penalty}, music.generation.semantic)
    negative = negative_prefix(plan.request, music.tokenizer, plan.abc_ids) if cfg_scale != 1 else None
    model = music.prepare(len(plan.prefix) + max_tokens, 1 if cfg_scale == 1 else 2)
    ids, timing, truncated = generate_tokens(model, plan.prefix, sampling, plan.request.seed, "semantic",
                                            negative=negative, cfg_scale=cfg_scale, legacy_off=plan.request.cot == "off",
                                            cancelled=cancelled, on_token=token_progress(max_tokens, "semantic"))
    if not ids:
        raise ValueError("YuE2 produced no music tokens. Try a different seed or lyrics.")
    if truncated:
        logging.warning("YuE2 reached max_duration; increase it if the song ends early.")
    logging.info(
        "YuE2: semantic done (%d tokens, %.1fs, %.1f tok/s); starting acoustic synthesis (%d steps)",
        timing.get("content_tokens", len(ids)), timing.get("seconds", 0.0), timing.get("output_tps", 0.0), steps,
    )
    latent = synthesize(model, plan.prefix, [token - CODEC_OFFSET for token in ids], plan.request.seed,
                        steps=steps, cancelled=cancelled, on_progress=acoustic_progress(steps))
    logging.info("YuE2: acoustic latents ready shape=%s", tuple(latent.shape))
    # The runtime owns YuE2's native [B,C,T] layout.
    return latent.T.unsqueeze(0).contiguous(), truncated, timing


def decode(vae, latent, tile_frames):
    if latent.ndim != 3 or latent.shape[1] != 64 or latent.shape[-1] == 0:
        raise ValueError("Expected YuE2 latents shaped [batch,64,frames]")
    tiles = (latent.shape[-1] + tile_frames - 1) // tile_frames
    logging.info("YuE2: decoding audio (%d frames, tile_frames=%d, %d tile(s))", latent.shape[-1], tile_frames, tiles)
    mm.load_models_gpu([vae], memory_required=2 * 1024**3, force_full_load=True)
    bar = ProgressBar(tiles)
    started = time.perf_counter()
    last_log = [started]

    def on_tile(completed, total=tiles):
        bar.update_absolute(completed)
        now = time.perf_counter()
        if completed == 1 or completed >= total or now - last_log[0] >= 1.0:
            logging.info("YuE2: decode tile %d / %d (%.1f%%)", completed, total, 100.0 * completed / max(total, 1))
            last_log[0] = now

    audio = vae.model.decode_tiled(latent, tile_frames, on_tile)
    if not torch.isfinite(audio).all():
        raise ValueError("YuE2 produced non-finite audio")
    logging.info("YuE2: decode complete in %.1fs (sample_rate=%s)", time.perf_counter() - started, vae.model.sample_rate)
    return {"waveform": audio.clamp_(-1, 1), "sample_rate": vae.model.sample_rate}
