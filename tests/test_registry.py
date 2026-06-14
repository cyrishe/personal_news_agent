from pathlib import Path

import pytest

from personal_news_agent.services.source_registry import SourceRegistryError, SourceRegistryService


def test_registry_loads_focused_categories_and_sources():
    registry = SourceRegistryService(Path("sources.yaml"))
    registry.load()

    assert len(registry.all_sources()) >= 20
    assert len(registry.get_sources_by_category("tech")) >= 4
    assert len(registry.get_sections_by_category("game")) >= 4
    assert "ithome.com" in registry.get_domain_filters(["tech"], None)
    summary = registry.source_summary()
    assert summary["source_count"] >= 20
    assert summary["tags"]["tech"] >= 4
    ithome = registry.get_source("ithome")
    assert "tech" in ithome.tags
    assert ithome.crawl_interval_minutes > 0
    assert registry.get_source("people_politics").crawl_interval_minutes == 20


def test_registry_rejects_invalid_category(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        """
sources:
  - source_id: bad
    name: Bad
    root_domain: example.com
    source_type: portal
    categories: [unknown]
    sections:
      - key: unknown
        name: Bad
        category: unknown
        url: https://example.com/
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        SourceRegistryService(path).load()


def test_registry_rejects_bad_url(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        """
sources:
  - source_id: bad
    name: Bad
    root_domain: example.com
    source_type: portal
    categories: [tech]
    sections:
      - key: tech
        name: Bad
        category: tech
        url: not-a-url
""",
        encoding="utf-8",
    )

    with pytest.raises(SourceRegistryError):
        SourceRegistryService(path).load()


def test_registry_selects_sources_for_profile():
    registry = SourceRegistryService(Path("sources.yaml"))
    registry.load()
    selected = registry.select_sources_for_profile(
        {
            "preferred_categories": ["sports"],
            "interests": ["NBA"],
            "self_description": "关心体育",
        }
    )
    assert selected
    assert all("sports" in source.categories for source in selected)
