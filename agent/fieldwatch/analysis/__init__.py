"""Field analyzer (WS1): `analyze(FieldAnalysisRequest) -> FieldAnalysisResult`.

Numeric core (`core.analyze_stacks`) works on numpy arrays; scenes come from an injectable loader
(`loader.StacLoader`: Earth Search STAC + COG windowed reads) and overlays go through an injected writer
(`writer.NullWriter` / `writer.LocalWriter`, `s3_writer.S3Writer`). No web or AWS-SDK imports here.
"""
from .analyzer import analyze

__all__ = ["analyze"]
