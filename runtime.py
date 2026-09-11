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
from .abc_score import normalize_native_abc, parse as parse_abc, AbcError


SPEED_PRESETS = {
    "fast": {"acoustic_steps": 16, "label": "fast"},
    "balanced": {"acoustic_steps": 24, "label": "balanced"},
    "quality": {"acoustic_steps": 32, "label": "quality"},
    "custom": {"acoustic_steps": None, "label": "custom"},
}


@dataclass(frozen=True)
class Plan:
    request: SongRequest
    abc: str | None
    abc_ids: list[int]
    prefix: list[int]
    truncated: bool = False
    score_seconds: float | None = None
    score_bars: int | None = None


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


def score_metrics(abc: str) -> tuple[float, int]:
    """Return (approximate seconds, bar count) for native ABC."""
    score = parse_abc(normalize_native_abc(abc))
    bars = len(score.voices["Vocal"].bars)
    seconds = float(score.voices["Vocal"].time * 60 / score.bpm)
    return seconds, bars


def resolve_speed_preset(preset: str, acoustic_steps: int) -> tuple[int, str]:
    key = (preset or "custom").lower()
    if key not in SPEED_PRESETS:
        raise ValueError("speed_preset must be fast, balanced, quality, or custom")
    mapped = SPEED_PRESETS[key]["acoustic_steps"]
    if mapped is None:
        return acoustic_steps, "custom"
    return int(mapped), key


def format_timing(timing: dict, truncated: bool, steps: int, preset: str, audio_seconds: float | None = None) -> str:
    status = "Duration limit reached — increase max_duration for a complete ending." if truncated else "Song generation complete."
    parts = [
        status,
        f"preset={preset}",
        f"acoustic_steps={steps}",
        f"AR {timing.get('content_tokens', 0)} tok in {timing.get('seconds', 0.0):.1f}s",
        f"{timing.get('output_tps', 0.0):.1f} tok/s",
        f"prefill {timing.get('prefill_seconds', 0.0):.2f}s",
    ]
    if audio_seconds is not None:
        parts.append(f"~{audio_seconds:.1f}s audio tokens")
    if timing.get("cfg_branches", 1) > 1:
        parts.append("CFG=2 branches (slower)")
    return " | ".join(parts)


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
        try:
            seconds, bars = score_metrics(request.abc)
        except AbcError as error:
            raise ValueError(f"Supplied score_abc failed YuE2 validation: {error}") from error
        ids = music.tokenizer.encode(request.abc)
        logging.info(
            "YuE2: compose using supplied ABC (%d tokens, %d bars, ~%.1fs musical length, planning=%s)",
            len(ids), bars, seconds, mode,
        )
        if seconds < 45:
            logging.warning(
                "YuE2: supplied score is only ~%.1fs (%d bars). Connected LLM/short ABC often yields ~20–30s songs. "
                "Use PROMPT_GENERATION_WITH_ABC.md targets (32–64 bars for ~2–3 min) or lengthen the score in Piano Roll.",
                seconds, bars,
            )
        return Plan(request, request.abc, ids, token_prefixes(request, music.tokenizer, ids),
                    score_seconds=seconds, score_bars=bars)
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
    decoded = music.tokenizer.decode(ids)
    try:
        seconds, bars = score_metrics(decoded)
    except AbcError:
        seconds, bars = None, None
    return Plan(request, decoded, ids, token_prefixes(request, music.tokenizer, ids),
                score_seconds=seconds, score_bars=bars)


def render(music, plan, max_seconds, temperature, top_p, top_k, repetition_penalty, cfg_scale, steps,
           speed_preset="custom"):
    if token_prefixes(plan.request, music.tokenizer, plan.abc_ids) != plan.prefix:
        raise ValueError("YuE2 plan changed. Submit edited ABC through the Plan node.")
    steps, preset = resolve_speed_preset(speed_preset, steps)
    max_tokens = round(max_seconds * 25)
    min_tokens = min(200, max_tokens)
    if plan.score_seconds is not None and plan.score_seconds + 5 < max_seconds * 0.5:
        logging.warning(
            "YuE2: score musical length ~%.1fs is much shorter than max_duration=%ds — output often ends near the score length. "
            "Lengthen the ABC (more sections/bars) or lower max_duration to match.",
            plan.score_seconds, max_seconds,
        )
    if cfg_scale != 1:
        logging.info("YuE2: guidance=%.2f enables a second CFG branch (~2x AR cost)", cfg_scale)
    logging.info(
        "YuE2: rendering semantic tokens (max_duration=%ds ~ %d tokens, min_tokens=%d, guidance=%.2f, steps=%d, preset=%s, seed=%d)",
        max_seconds, max_tokens, min_tokens, cfg_scale, steps, preset, plan.request.seed,
    )
    sampling = resolve_sampling({"max_tokens": max_tokens, "min_tokens": min_tokens,
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
    audio_seconds = len(ids) / 25.0
    codec = [token - CODEC_OFFSET for token in ids]
    if any(token < 0 or token >= CODEC_SIZE for token in codec):
        raise ValueError("YuE2 produced semantic IDs outside the codec band")
    unique = len(set(codec))
    logging.info(
        "YuE2: semantic done (%d tokens, %d unique codec ids, range %d-%d, ~%.1fs audio, %.1fs wall, %.1f tok/s); "
        "starting acoustic synthesis (%d steps, preset=%s)",
        timing.get("content_tokens", len(ids)), unique, min(codec), max(codec),
        audio_seconds, timing.get("seconds", 0.0), timing.get("output_tps", 0.0), steps, preset,
    )
    if unique < 8:
        logging.warning(
            "YuE2: only %d unique codec tokens — output may be silent or degenerate. Try another seed or planning=off.",
            unique,
        )
    latent = synthesize(model, plan.prefix, codec, plan.request.seed,
                        steps=steps, cancelled=cancelled, on_progress=acoustic_progress(steps))
    logging.info(
        "YuE2: acoustic latents ready shape=%s |mean|=%.5f |max|=%.5f",
        tuple(latent.shape), float(latent.abs().mean()), float(latent.abs().max()),
    )
    timing = {**timing, "acoustic_steps": steps, "speed_preset": preset, "audio_seconds": audio_seconds}
    # The runtime owns YuE2's native [B,C,T] layout.
    return latent.T.unsqueeze(0).contiguous(), truncated, timing


def decode(vae, latent, tile_frames):
    if latent.ndim != 3 or latent.shape[1] != 64 or latent.shape[-1] == 0:
        raise ValueError("Expected YuE2 latents shaped [batch,64,frames]")
    tiles = (latent.shape[-1] + tile_frames - 1) // tile_frames
    logging.info(
        "YuE2: decoding audio (%d frames, tile_frames=%d, %d tile(s), latent |mean|=%.5f |max|=%.5f)",
        latent.shape[-1], tile_frames, tiles, float(latent.detach().abs().mean()), float(latent.detach().abs().max()),
    )
    if float(latent.detach().abs().max()) < 1e-4:
        raise ValueError(
            "YuE2 acoustic latents are near-zero (silent). Try planning=off, a different seed, "
            "or paste a validated Score ABC in Piano Roll — auto-composed short scores sometimes yield empty audio."
        )
    mm.load_models_gpu([vae], memory_required=2 * 1024**3, force_full_load=True)
    bar = ProgressBar(tiles)
    started = time.perf_counter()
    audio = vae.model.decode_tiled(latent, tile_frames, bar.update_absolute)
    if not torch.isfinite(audio).all():
        raise ValueError("YuE2 produced non-finite audio")
    peak = float(audio.detach().abs().max())
    rms = float(audio.detach().pow(2).mean().sqrt())
    logging.info(
        "YuE2: decode complete in %.1fs (sample_rate=%s, peak=%.4f, rms=%.6f)",
        time.perf_counter() - started, vae.model.sample_rate, peak, rms,
    )
    if peak < 0.01:
        raise ValueError(
            "YuE2 decoded near-silent audio (peak=%.4f). This often happens with auto-composed ABC "
            "and no pasted score. Retry with planning=off, another seed, shorter max_duration (e.g. 60), "
            "or supply Score ABC via Piano Roll / the no-JSON prompt for lyrics+style only."
            % peak
        )
    return {"waveform": audio.clamp_(-1, 1), "sample_rate": vae.model.sample_rate}
