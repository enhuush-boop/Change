from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr

InputLanguage = Literal["auto", "mn_cyrl", "mn_latn", "en"]
SourceScope = Literal["auto", "medical", "fact_check", "all"]


class FactCheckRequest(BaseModel):
    claim: str = Field(min_length=3, max_length=10_000)
    language: InputLanguage = "auto"
    source_scope: SourceScope = "auto"


class TextRequest(BaseModel):
    # Short inputs are allowed so the quality gate can explain why they were
    # not sent to the model, instead of returning a schema validation error.
    text: str = Field(min_length=1, max_length=100_000)
    language: InputLanguage = "auto"


class UrlRequest(BaseModel):
    url: str = Field(min_length=4, max_length=4_000)
    virustotal_api_key: SecretStr | None = None


class UrlCheckRequest(BaseModel):
    url: str = Field(min_length=4, max_length=4_000)
    language: InputLanguage = "auto"
    source_scope: SourceScope = "auto"
    virustotal_api_key: SecretStr | None = None


class VirusTotalKeyRequest(BaseModel):
    api_key: SecretStr


class ImageUrlRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4_000)
    # Off by default: tiling runs one model inference per region.
    tiles: bool = False


class VideoUrlRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4_000)
