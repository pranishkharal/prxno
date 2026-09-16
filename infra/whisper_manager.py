"""
Shared Whisper model management.

``captioning.get_whisper_model()`` cached one module-global model with no lock,
so two worker threads could both observe ``None`` and load the model twice,
doubling RAM (and on a GPU box, doubling VRAM).

This manager guarantees a single instance per
(backend, model, device, compute_type) key, loads lazily, serialises inference
around the loaded instance, and derives the device and compute type from the
detected hardware instead of assuming CUDA exists.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, Optional, Tuple

from .hardware import HardwareProfile
from .logutil import log_event


def resolve_whisper_settings(profile: Optional[HardwareProfile] = None) -> Tuple[str, str, str, str]:
    """Return ``(backend, model_name, device, compute_type)`` for this machine."""
    if profile is None:
        profile = HardwareProfile.detect(probe_encoders=False, use_cache=True)

    backend = (os.getenv("WHISPER_BACKEND") or "openai").strip().lower()
    model_name = (os.getenv("WHISPER_MODEL") or "base").strip()
    device = (os.getenv("WHISPER_DEVICE") or "").strip().lower()
    compute_type = (os.getenv("WHISPER_COMPUTE_TYPE") or "").strip()

    if not device:
        device = "cuda" if profile.cuda_available else "cpu"
    if not compute_type:
        compute_type = "float16" if device == "cuda" else "int8"
    return backend, model_name, device, compute_type


class WhisperManager:
    """Owns the process-wide transcription model."""

    def __init__(self, profile: Optional[HardwareProfile] = None):
        self.profile = profile or HardwareProfile.detect(probe_encoders=False, use_cache=True)
        self._load_lock = threading.RLock()
        self._inference_lock = threading.Lock()
        self._models: Dict[Tuple[str, str, str, str], Any] = {}

    def describe(self) -> Dict[str, Any]:
        backend, model_name, device, compute_type = resolve_whisper_settings(self.profile)
        return {
            "backend": backend,
            "model": model_name,
            "device": device,
            "compute_type": compute_type,
            "loaded": len(self._models),
        }

    def _resolve(self, backend, model_name, device, compute_type):
        defaults = resolve_whisper_settings(self.profile)
        return (
            (backend or defaults[0]),
            (model_name or defaults[1]),
            (device or defaults[2]),
            (compute_type or defaults[3]),
        )

    def get_model(
        self,
        backend: Optional[str] = None,
        model_name: Optional[str] = None,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
    ):
        """Return the shared model, loading it once under a lock."""
        key = self._resolve(backend, model_name, device, compute_type)

        with self._load_lock:
            model = self._models.get(key)
            if model is not None:
                return model
            model = self._load(key)
            self._models[key] = model
            return model

    def _load(self, key: Tuple[str, str, str, str]):
        backend, model_name, device, compute_type = key
        log_event("WHISPER_LOAD_STARTED", backend=backend, model=model_name,
                  device=device, compute_type=compute_type)
        try:
            model = self._load_uncached(backend, model_name, device, compute_type)
        except Exception as error:
            if device != "cpu":
                log_event("WHISPER_LOAD_FALLBACK", device=device, reason=str(error))
                model = self._load_uncached(backend, model_name, "cpu", "int8")
            else:
                raise
        log_event("WHISPER_LOAD_COMPLETED", backend=backend, model=model_name, device=device)
        return model

    @staticmethod
    def _load_uncached(backend: str, model_name: str, device: str, compute_type: str):
        if backend in ("faster", "faster-whisper", "faster_whisper"):
            from faster_whisper import WhisperModel  # noqa: WPS433 - optional backend
            return WhisperModel(model_name, device=device, compute_type=compute_type)
        import whisper  # noqa: WPS433 - heavyweight, imported only when needed
        return whisper.load_model(model_name, device=device)

    def transcribe(self, path, language: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """Transcribe ``path`` with the shared model.

        Inference is serialised around the shared instance, because neither
        backend guarantees that a single model object is safe for concurrent
        calls.
        """
        backend, model_name, device, compute_type = resolve_whisper_settings(self.profile)
        model = self.get_model(backend, model_name, device, compute_type)

        with self._inference_lock:
            if backend in ("faster", "faster-whisper", "faster_whisper"):
                segments, info = model.transcribe(str(path), language=language, **kwargs)
                collected = list(segments)
                text = " ".join(getattr(s, "text", "").strip() for s in collected).strip()
                return {
                    "text": text,
                    "language": getattr(info, "language", language),
                    "segments": [
                        {
                            "start": float(getattr(s, "start", 0.0)),
                            "end": float(getattr(s, "end", 0.0)),
                            "text": getattr(s, "text", ""),
                        }
                        for s in collected
                    ],
                }
            return model.transcribe(str(path), language=language, **kwargs)

    def unload(self) -> int:
        """Drop every loaded model (frees RAM)."""
        with self._load_lock:
            count = len(self._models)
            self._models.clear()
        return count


_MANAGER: Optional[WhisperManager] = None
_MANAGER_LOCK = threading.Lock()


def get_whisper_manager() -> WhisperManager:
    """Process-wide Whisper manager."""
    global _MANAGER
    if _MANAGER is None:
        with _MANAGER_LOCK:
            if _MANAGER is None:
                _MANAGER = WhisperManager()
    return _MANAGER


def get_shared_model():
    """Convenience accessor matching the old ``get_whisper_model`` contract."""
    return get_whisper_manager().get_model()