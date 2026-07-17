"""Tests for documented package-level imports."""

from __future__ import annotations


def test_readme_pipeline_imports_are_public():
    from terravault import CDSEDownloadAuthConfig, Pipeline, PipelineConfig

    assert CDSEDownloadAuthConfig is not None
    assert Pipeline is not None
    assert PipelineConfig is not None


def test_historical_imports_are_public():
    from terravault import HistoricalConfig, HistoricalIngestor, HistoricalRunResult

    assert HistoricalConfig is not None
    assert HistoricalIngestor is not None
    assert HistoricalRunResult is not None
