#!/usr/bin/env python3
"""Fetch the models the native engines need, ahead of time, so the first utterance is not slow.

  Kokoro   kokoro-v1.0.onnx (326 MB) and voices-v1.0.bin (28 MB) from the kokoro-onnx GitHub release model-files-v1.0,
           into <TALKOVER_HOME>/models/kokoro. Each file is checked against the release's size and SHA-256; a
           download goes to <name>.part first and resumes from there if it was interrupted.
  Whisper  the model the server will load (mlx-community/whisper-small-mlx on Apple Silicon, Systran/faster-whisper-small
           elsewhere; see model_store.py for the settings), as a Hugging Face snapshot in <TALKOVER_HOME>/models/whisper.

Idempotent: files that are already there and intact are left alone. Prints what it did. Exit code 0 = everything is
in place, 1 = something could not be fetched (run it again; partial downloads resume).

Run:  python engines/native/fetch_models.py [--stt-only | --tts-only] [--stt-model REPO_ID]
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_store  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--stt-only", action="store_true", help="only the Whisper model")
    g.add_argument("--tts-only", action="store_true", help="only the Kokoro files")
    p.add_argument("--stt-model", help="Whisper model repo id (default: the one the server will load)")
    args = p.parse_args(argv)
    s = model_store.Settings.from_env()
    print("Talkover native models in %s" % s.models_dir)
    ok = True

    if not args.stt_only:
        t0 = time.monotonic()
        try:
            r = model_store.ensure_kokoro(s, verify=True)
            for name, how in r.items():
                size = model_store.KOKORO_FILES[name][0]
                print("OK    kokoro  %-24s %s (%.0f MB, SHA-256 checked)" % (name, how, size / 1e6))
            print("      kokoro  %s (%.1f s)" % (s.kokoro_dir, time.monotonic() - t0))
        except Exception as e:
            ok = False
            print("FAIL  kokoro  %s" % e)

    if not args.tts_only:
        model = model_store.translate_model_id(args.stt_model, s.stt_backend) if args.stt_model else s.stt_model
        t0 = time.monotonic()
        try:
            path, how = model_store.ensure_whisper(model, s.whisper_dir)
            print("OK    whisper %-24s %s (%.0f MB, for %s)" % (model, how, model_store.folder_mb(path), s.stt_backend))
            print("      whisper %s (%.1f s)" % (path, time.monotonic() - t0))
        except Exception as e:
            ok = False
            print("FAIL  whisper %s: %s" % (model, e))

    print("RESULT: %s" % ("all models in place" if ok else "something is missing; run again (downloads resume)"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
