#!/usr/bin/env python3
"""
Whisper transcription wrapper - called as a subprocess for each video
Usage: python whisper_transcribe.py <video_path> <model_size> [output_file]
"""
import sys
import os
import json

os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'

from faster_whisper import WhisperModel

def transcribe(video_path, model_size='medium'):
    """Transcribe video audio using Whisper"""
    # Check if model is already downloaded
    model_path = os.path.expanduser(f'~/.cache/huggingface/hub/models--Systran--faster-whisper-{model_size}')
    
    if not os.path.exists(model_path):
        print(f"Model {model_size} not found in cache", file=sys.stderr)
        sys.exit(1)
    
    print(f"Loading {model_size} model...", file=sys.stderr)
    model = WhisperModel(model_size, device='cpu', compute_type='int8')
    
    print(f"Transcribing: {video_path}", file=sys.stderr)
    segments, info = model.transcribe(
        video_path,
        beam_size=5,
        language='zh'
    )
    text = ' '.join([s.text for s in segments])
    return text

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: python whisper_transcribe.py <video_path> <model_size> [output_file]")
        sys.exit(1)
    
    video_path = sys.argv[1]
    model_size = sys.argv[2]
    output_file = sys.argv[3] if len(sys.argv) > 3 else None
    
    text = transcribe(video_path, model_size)
    
    if output_file:
        with open(output_file, 'w') as f:
            f.write(text)
        print(f"Transcript saved to {output_file} ({len(text)} chars)", file=sys.stderr)
    else:
        print(text)
