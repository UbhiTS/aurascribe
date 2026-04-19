"""faster-whisper ASR + pyannote speaker diarization.

One pipeline does all the speaker work:
- For each VAD chunk, `pyannote/speaker-diarization-3.1` returns
  speaker-turn boundaries + a centroid embedding per detected speaker.
- We slice audio per turn, run whisper on each slice, and use the
  pipeline's own 256-dim centroid as the embedding — no separate
  `pyannote/embedding` inference needed. Voice tags use the same
  pipeline so every embedding lives in one vector space.

If the pipeline fails to load (licence not accepted, HF_TOKEN missing,
or the `[diarization]` extra not installed), ASR still works; speaker
fields come back as "Unknown" with no embedding attached.
"""
from __future__ import annotations

import asyncio
import logging
import pickle
from typing import Awaitable, Callable

import aiosqlite
import numpy as np

from aurascribe.config import (
    DB_PATH,
    DIARIZATION_MODEL,
    HF_TOKEN,
    MODELS_DIR,
    SAMPLE_RATE,
    WHISPER_COMPUTE_TYPE,
    WHISPER_DEVICE,
    WHISPER_LANGUAGE,
    WHISPER_MODEL,
)
from aurascribe.transcription.engine import PartialCallback, Utterance

log = logging.getLogger("aurascribe.whisper")

# Stage-progress callback, awaited between load phases so the caller can
# broadcast status to the frontend.
StageCallback = Callable[[str], Awaitable[None]]


def _is_whisper_cached(model_name: str) -> bool:
    """Best-effort check for a pre-downloaded whisper model in MODELS_DIR.

    faster-whisper uses HuggingFace's cache layout: weights live under
    `MODELS_DIR/models--<org>--<repo>/snapshots/<sha>/`. When the file
    layout changes across versions the guess may be wrong; the worst case
    is that we show "Downloading..." for a model that's actually already
    cached, which is merely misleading — it still loads fine.
    """
    try:
        for entry in MODELS_DIR.iterdir():
            name = entry.name.lower()
            if not name.startswith("models--"):
                continue
            if model_name.lower() in name:
                # Has at least one real weight file inside?
                for f in entry.rglob("*"):
                    if f.suffix in (".bin", ".safetensors") and f.stat().st_size > 1_000_000:
                        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False
    return False

# Cosine-distance thresholds. Edit here to retune speaker identification.
_THRESH_MULTI = 0.55
_THRESH_SOLO = 0.70
# Ratio test: best speaker must beat second-best by this margin. Rejects
# ambiguous chunks where two voices are near-tied.
_RATIO_MARGIN = 0.80
# Min embeddings a Voice needs before it participates in auto-matching. One
# or two tagged snippets isn't enough signal — the k-NN match is unstable
# and fires false-positives. Below the gate, a Voice only applies when the
# user directly tags a line; live auto-ID stays silent until the pool grows.
_MIN_VOICE_SAMPLES = 3


def _valid_embedding(emb) -> bool:
    """Check an embedding is safe for cosine distance — finite + non-zero norm."""
    try:
        arr = np.asarray(emb, dtype=np.float32)
    except Exception:
        return False
    if arr.size == 0 or not np.all(np.isfinite(arr)):
        return False
    return bool(np.linalg.norm(arr) > 1e-8)


class WhisperEngine:
    """ASR via faster-whisper; speaker ID via pyannote diarization pipeline."""

    # A diarized chunk can produce many turns. Sub-segments shorter than this
    # produce whisper hallucinations, so we drop the whole turn.
    _MIN_TURN_SEC = 0.4
    # Minimum turn duration for the embedding to be trustworthy enough to
    # cluster. Below this, transcribe the turn but don't use its centroid —
    # pyannote's 1-2s embeddings drift enough that a same-speaker turn can
    # sit ~0.9 from the existing centroid, which would spawn a phantom
    # Speaker N. Turns render as "Unknown" and can be tagged manually.
    _MIN_EMBED_TURN_SEC = 1.5

    def __init__(self, enable_speaker_id: bool = True) -> None:
        self._model = None
        self._diarization_pipeline = None
        self._enable_speaker_id = enable_speaker_id
        # {voice_name: [embedding, ...]} — the pool accumulates tagged
        # snippets as the user assigns utterances to voices.
        self._voice_pools: dict[str, list[np.ndarray]] = {}
        # Records where pyannote's pipeline actually landed after load:
        # "cuda" if we successfully moved it to the GPU, "cpu" if it loaded
        # but torch couldn't see a GPU (common when torch is CPU-only but
        # ctranslate2 has CUDA), or None when the pipeline failed to load
        # entirely (no HF token, licence not accepted, extras missing).
        self._diarization_device: str | None = None
        self._ready = False

    async def load(self, on_stage: "StageCallback | None" = None) -> None:
        """Load whisper + diarization models. `on_stage` (optional) is
        awaited before each heavy phase with a short human-readable label
        — the MeetingManager uses it to broadcast fine-grained status
        updates so the splash doesn't look hung during the ~2 min first
        run while Whisper and pyannote weights download."""
        loop = asyncio.get_running_loop()

        # Whisper phase. The first load for a given `whisper_model` is a
        # download (~100 MB to 1.6 GB depending on size); subsequent loads
        # are instant reads from MODELS_DIR. We detect the cached case by
        # looking for the HF snapshot dir so we can set the right message.
        if on_stage:
            cached = _is_whisper_cached(WHISPER_MODEL)
            verb = "Loading" if cached else "Downloading"
            await on_stage(
                f"{verb} Whisper model '{WHISPER_MODEL}'... "
                f"({'instant' if cached else '1–5 min, one-time'})"
            )
        await loop.run_in_executor(None, self._load_whisper_sync)

        # Diarization phase. Pyannote downloads the segmentation + embedding
        # weights on first use; total ~200 MB. Also potentially slow on a
        # cold cache but much quicker than whisper.
        if self._enable_speaker_id:
            if on_stage:
                await on_stage("Loading speaker diarization pipeline...")
            await loop.run_in_executor(None, self._load_diarization_sync)

        await self._load_voices()
        self._ready = True

    def _load_whisper_sync(self) -> None:
        from faster_whisper import WhisperModel

        log.info(
            "Loading faster-whisper: model=%s device=%s compute=%s",
            WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE,
        )
        self._model = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
            download_root=str(MODELS_DIR),
        )

    def _load_diarization_sync(self) -> None:
        try:
            from pyannote.audio import Pipeline
            import torch

            log.info("Loading %s", DIARIZATION_MODEL)
            pipe = Pipeline.from_pretrained(DIARIZATION_MODEL, token=HF_TOKEN)
            if WHISPER_DEVICE == "cuda" and torch.cuda.is_available():
                pipe.to(torch.device("cuda"))
                self._diarization_device = "cuda"
            else:
                # Pipeline loaded but we can't move it to GPU — either the
                # user configured cpu, or torch is CPU-only. Either way,
                # pyannote runs on CPU and inference is ~10× slower than
                # the GPU path. Log so the user knows what to fix.
                self._diarization_device = "cpu"
                if WHISPER_DEVICE == "cuda":
                    log.info(
                        "diarization: staying on CPU (torch.cuda.is_available()=False). "
                        "Install a CUDA-enabled torch wheel to move diarization to GPU: "
                        "pip install --index-url https://download.pytorch.org/whl/cu121 torch torchaudio"
                    )
            self._diarization_pipeline = pipe
        except Exception as e:
            log.warning(
                "Diarization disabled (pipeline unavailable — accept license at "
                "https://hf.co/%s and set HF_TOKEN): %s",
                DIARIZATION_MODEL, e,
            )
            self._diarization_pipeline = None
            self._diarization_device = None

    async def _load_voices(self) -> None:
        pools: dict[str, list[np.ndarray]] = {}
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT v.name, ve.embedding FROM voice_embeddings ve "
                    "JOIN voices v ON v.id = ve.voice_id"
                )
                async for name, emb_bytes in cursor:
                    if emb_bytes is None:
                        continue
                    pools.setdefault(name, []).append(pickle.loads(emb_bytes))
        except Exception as e:
            log.warning("Could not load voices: %s", e)
        self._voice_pools = pools
        log.info(
            "Voices loaded: %s",
            {name: len(pool) for name, pool in pools.items()},
        )

    async def reload_voices(self) -> None:
        await self._load_voices()

    # ── Runtime introspection (surfaced in /api/status for the header) ───────

    @property
    def whisper_model(self) -> str:
        return WHISPER_MODEL

    @property
    def whisper_device(self) -> str:
        return WHISPER_DEVICE

    @property
    def whisper_compute_type(self) -> str:
        return WHISPER_COMPUTE_TYPE

    @property
    def diarization_device(self) -> str | None:
        """Where pyannote actually landed, or None when disabled."""
        return self._diarization_device

    async def transcribe(
        self,
        audio: np.ndarray,
        on_partial: PartialCallback | None = None,
        *,
        diarize: bool = True,
    ) -> list[Utterance]:
        if not self._ready:
            raise RuntimeError("Engine not loaded — call load() first")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._transcribe_sync, audio, on_partial, diarize
        )

    async def diarize_full_audio(
        self, audio: np.ndarray
    ) -> list[tuple[float, float, str, float | None]]:
        """Run pyannote on an entire meeting's audio in one pass.

        Unlike the live path (which runs per VAD chunk), this sees the whole
        conversation at once — the extra context lets pyannote split clusters
        it previously merged within a chunk. Used by the Recompute endpoint to
        re-label past meetings after the Voices DB has grown.

        Returns [(start, end, voice_name_or_Unknown, match_distance)]. Times
        are in seconds relative to the start of `audio`. The caller maps
        these onto stored utterances via their `audio_start` field.
        """
        if self._diarization_pipeline is None:
            raise RuntimeError(
                "Diarization pipeline unavailable — accept the license at "
                f"https://hf.co/{DIARIZATION_MODEL} and set HF_TOKEN."
            )
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._diarize_full_audio_sync, audio)

    def _diarize_full_audio_sync(
        self, audio: np.ndarray
    ) -> list[tuple[float, float, str, float | None]]:
        import torch

        waveform = torch.from_numpy(audio).unsqueeze(0)
        result = self._diarization_pipeline(
            {"waveform": waveform, "sample_rate": SAMPLE_RATE}
        )

        annotation = result
        for attr in ("exclusive_speaker_diarization", "speaker_diarization", "annotation"):
            if hasattr(result, attr):
                annotation = getattr(result, attr)
                break
        if not hasattr(annotation, "itertracks"):
            log.warning(
                "Full diarization returned unexpected type %s",
                type(result).__name__,
            )
            return []

        # Resolve each local label to a voice once, using its centroid.
        label_embeddings: dict[str, np.ndarray] = {}
        emb_matrix = getattr(result, "speaker_embeddings", None)
        if emb_matrix is not None:
            try:
                source = getattr(result, "speaker_diarization", annotation)
                labels = list(source.labels())
                arr = np.asarray(emb_matrix)
                for i, lbl in enumerate(labels):
                    if i < arr.shape[0]:
                        label_embeddings[str(lbl)] = arr[i]
            except Exception as e:
                log.warning("Could not harvest full-meeting embeddings: %s", e)

        label_resolution: dict[str, tuple[str, float | None]] = {}
        for lbl, emb in label_embeddings.items():
            if not _valid_embedding(emb):
                label_resolution[lbl] = ("Unknown", None)
                continue
            speaker, distance = self._match_speaker(emb)
            label_resolution[lbl] = (speaker, distance)

        turns: list[tuple[float, float, str, float | None]] = []
        for segment, _track, label in annotation.itertracks(yield_label=True):
            start = float(segment.start)
            end = float(segment.end)
            speaker, distance = label_resolution.get(str(label), ("Unknown", None))
            turns.append((start, end, speaker, distance))
        turns.sort(key=lambda t: t[0])
        return turns

    def _transcribe_sync(
        self,
        audio: np.ndarray,
        on_partial: PartialCallback | None = None,
        diarize: bool = True,
    ) -> list[Utterance]:
        if diarize and self._diarization_pipeline is not None:
            turns, label_embeddings = self._diarize(audio)
            if turns:
                return self._transcribe_per_turn(audio, turns, label_embeddings, on_partial)
        # diarize=False (speculative partial), diarization unavailable, or no
        # speech detected → ASR only, no embedding, speaker="Unknown".
        return self._transcribe_asr_only(audio, on_partial)

    def _transcribe_asr_only(
        self, audio: np.ndarray, on_partial: PartialCallback | None
    ) -> list[Utterance]:
        segments, _info = self._model.transcribe(
            audio,
            beam_size=5,
            language=WHISPER_LANGUAGE,
            condition_on_previous_text=False,
            vad_filter=False,  # audio is already VAD-gated upstream
        )
        utterances: list[Utterance] = []
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            utterances.append(
                Utterance(
                    speaker="Unknown",
                    text=text,
                    start=seg.start,
                    end=seg.end,
                    embedding=None,
                )
            )
            if on_partial:
                on_partial("Unknown", text)
        return utterances

    def _diarize(
        self, audio: np.ndarray
    ) -> tuple[list[tuple[float, float, str]], dict[str, np.ndarray]]:
        """Run diarization; return (turns, label_embeddings).

        - `turns`: [(start, end, local_label)] with adjacent same-speaker turns
          merged (<=0.3s gap).
        - `label_embeddings`: {local_label: 256-dim centroid} from
          `DiarizeOutput.speaker_embeddings`.

        Returns ([], {}) on failure so the caller falls back to ASR-only.
        """
        try:
            import torch

            waveform = torch.from_numpy(audio).unsqueeze(0)
            result = self._diarization_pipeline(
                {"waveform": waveform, "sample_rate": SAMPLE_RATE}
            )
        except Exception as e:
            log.warning("Diarization failed, falling back to ASR-only: %s", e)
            return [], {}

        # The transcription-friendly (non-overlapping) annotation is
        # `exclusive_speaker_diarization`. Probe attributes so we survive
        # pyannote version drift.
        annotation = result
        for attr in ("exclusive_speaker_diarization", "speaker_diarization", "annotation"):
            if hasattr(result, attr):
                annotation = getattr(result, attr)
                break

        if not hasattr(annotation, "itertracks"):
            log.warning(
                "Diarization returned unexpected type %s — falling back to ASR-only",
                type(result).__name__,
            )
            return [], {}

        # Harvest per-speaker centroid embeddings. Ordered the same as
        # `speaker_diarization.labels()` per pyannote's DiarizeOutput docs.
        label_embeddings: dict[str, np.ndarray] = {}
        emb_matrix = getattr(result, "speaker_embeddings", None)
        if emb_matrix is not None:
            try:
                source = getattr(result, "speaker_diarization", annotation)
                labels = list(source.labels())
                arr = np.asarray(emb_matrix)
                for i, lbl in enumerate(labels):
                    if i < arr.shape[0]:
                        label_embeddings[str(lbl)] = arr[i]
            except Exception as e:
                log.warning("Could not harvest diarization embeddings: %s", e)

        raw: list[tuple[float, float, str]] = []
        for segment, _track, label in annotation.itertracks(yield_label=True):
            raw.append((float(segment.start), float(segment.end), str(label)))
        raw.sort(key=lambda x: x[0])

        # Merge abutting same-speaker turns (<= 0.3s gap).
        merged: list[tuple[float, float, str]] = []
        for start, end, label in raw:
            if merged and merged[-1][2] == label and start - merged[-1][1] <= 0.3:
                ps, _, _ = merged[-1]
                merged[-1] = (ps, end, label)
            else:
                merged.append((start, end, label))
        return merged, label_embeddings

    def _transcribe_per_turn(
        self,
        audio: np.ndarray,
        turns: list[tuple[float, float, str]],
        label_embeddings: dict[str, np.ndarray],
        on_partial: PartialCallback | None,
    ) -> list[Utterance]:
        log.info(
            "diarize: %d turns (%s)",
            len(turns),
            [(round(s, 2), round(e, 2), lbl) for s, e, lbl in turns],
        )

        # Phase 1 — resolve each unique local label to a speaker using only
        # long, valid-embedding turns. Short turns within the same chunk
        # inherit this resolution rather than being marked "Unknown".
        label_resolution: dict[str, tuple[str, np.ndarray, float | None]] = {}
        for start, end, local_label in turns:
            if local_label in label_resolution:
                continue
            if end - start < self._MIN_EMBED_TURN_SEC:
                continue
            embedding = label_embeddings.get(local_label)
            if embedding is None:
                continue
            if not _valid_embedding(embedding):
                log.warning(
                    "discarding degenerate embedding for %s (%.2f-%.2f)",
                    local_label, start, end,
                )
                continue
            speaker, distance = self._match_speaker(embedding)
            label_resolution[local_label] = (speaker, embedding, distance)

        # Phase 2 — emit utterances in timeline order. A turn's speaker comes
        # from Phase 1 when the label resolved; otherwise "Unknown". Embedding
        # is attached to exactly one turn per label (the first) so cross-chunk
        # clustering doesn't double-count the same centroid.
        utterances: list[Utterance] = []
        embedding_emitted: set[str] = set()
        for start, end, local_label in turns:
            if end - start < self._MIN_TURN_SEC:
                continue
            s_idx = int(start * SAMPLE_RATE)
            e_idx = int(end * SAMPLE_RATE)
            slice_audio = audio[s_idx:e_idx]
            if slice_audio.size < int(SAMPLE_RATE * self._MIN_TURN_SEC):
                continue

            resolved = label_resolution.get(local_label)
            if resolved is None:
                speaker = "Unknown"
                embedding_bytes = None
                match_distance: float | None = None
            else:
                speaker, embedding, match_distance = resolved
                if local_label in embedding_emitted:
                    embedding_bytes = None
                else:
                    embedding_bytes = pickle.dumps(embedding)
                    embedding_emitted.add(local_label)
                if end - start < self._MIN_EMBED_TURN_SEC:
                    log.info(
                        "inherited %s for short turn %s (%.2f-%.2f)",
                        speaker, local_label, start, end,
                    )

            segments, _info = self._model.transcribe(
                slice_audio,
                beam_size=5,
                language=WHISPER_LANGUAGE,
                condition_on_previous_text=False,
                vad_filter=False,
            )
            for seg in segments:
                text = seg.text.strip()
                if not text:
                    continue
                utterances.append(
                    Utterance(
                        speaker=speaker,
                        text=text,
                        # Rebase to chunk-relative time — caller adds the
                        # meeting-wide `elapsed` offset.
                        start=start + seg.start,
                        end=start + seg.end,
                        embedding=embedding_bytes,
                        match_distance=match_distance,
                    )
                )
                # Embedding belongs to the first emitted utterance of this
                # label, not to every ASR segment within it.
                embedding_bytes = None
                if on_partial:
                    on_partial(speaker, text)
        return utterances

    # ── Voice matching ────────────────────────────────────────────────────────

    def _match_speaker(self, embedding) -> tuple[str, float | None]:
        """Match `embedding` against the Voices pool.

        Returns (voice_name, distance). `distance` is the cosine distance to
        the winning centroid when we're confident enough to name a voice;
        None when the result is "Unknown" (no match).

        A Voice only participates once it has ≥ _MIN_VOICE_SAMPLES embeddings
        — fewer than that, the pool isn't stable enough to auto-assign.
        """
        # Filter voices that haven't passed the min-samples gate. Apply it
        # before the matching math so an under-gated voice can't win just by
        # being alone in the pool.
        eligible: dict[str, list[np.ndarray]] = {
            name: pool
            for name, pool in self._voice_pools.items()
            if len(pool) >= _MIN_VOICE_SAMPLES
        }
        if not eligible:
            return "Unknown", None

        from scipy.spatial.distance import cosine

        # For each eligible voice, take the MIN distance across their pool.
        per_speaker: list[tuple[str, float]] = []
        for name, pool in eligible.items():
            best = min(float(cosine(embedding, emb)) for emb in pool)
            per_speaker.append((name, best))
        if not per_speaker:
            return "Unknown", None
        per_speaker.sort(key=lambda nd: nd[1])

        only_one = len(per_speaker) == 1
        best_name, best_dist = per_speaker[0]
        second_dist = per_speaker[1][1] if len(per_speaker) > 1 else float("inf")
        threshold = _THRESH_SOLO if only_one else _THRESH_MULTI

        passes_abs = best_dist < threshold
        passes_ratio = only_one or (best_dist < _RATIO_MARGIN * second_dist)
        matched = passes_abs and passes_ratio
        decision = best_name if matched else "Unknown"

        log.info(
            "voice-match: per_voice=%s best=%s dist=%.3f second=%.3f "
            "thresh=%.2f ratio_margin=%.2f abs=%s ratio=%s -> %s",
            [(n, round(d, 3)) for n, d in per_speaker],
            best_name, best_dist, second_dist, threshold, _RATIO_MARGIN,
            passes_abs, passes_ratio, decision,
        )
        return (decision, best_dist if matched else None)
