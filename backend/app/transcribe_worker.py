"""Isolated local ASR process; never downloads a model automatically."""

import json
import sys


def main():
    from faster_whisper import WhisperModel

    config = json.loads(sys.argv[1])
    model = WhisperModel(
        config["model_path"], device=config["device"], compute_type="float32", local_files_only=True
    )
    segments, info = model.transcribe(
        config["audio_path"],
        language=config.get("language") or None,
        beam_size=5,
        vad_filter=True,
        word_timestamps=True,
    )
    print(
        json.dumps(
            {
                "language": info.language,
                "segments": [
                    {"start": segment.start, "end": segment.end, "text": segment.text.strip()}
                    for segment in segments
                ],
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
