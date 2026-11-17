"""wattson-shield: the small prompt-injection model, in its own pod (docs/study/guardrails.md).

server.py is the whole program. It imports nothing of wattson: its image (Dockerfile.shield)
holds Python, onnxruntime, tokenizers, numpy, Starlette, uvicorn and that one file."""
