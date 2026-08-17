"""Writing a tier slug back to the user config (TD-1703).

The settings screen lets a slug be edited, and the edit has to land in the
user's own ``config.yaml`` without destroying it.  That file is mostly
teaching: the block above ``local:`` explains why ``context_window`` is a
compaction budget and not a request parameter, and a PyYAML round-trip would
drop every word of it.  So the write is surgical, and these runs are mostly
about what it must *not* disturb.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tstd.config import ConfigError, ensure_user_config, load_config
from tstd.config_write import save_tier_slug

# A tag shaped like a real one: the colon is why the value is written quoted.
_TAG = "qwen3.8:27b"


def _user_config(tmp_path: Path) -> Path:
    """A fresh copy of the shipped config, as a first run would leave it."""
    return ensure_user_config(tmp_path / "config.yaml")


def _tier_block(path: Path, preset: str, tier: str) -> dict[str, object]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    block = data["presets"][preset][tier]
    assert isinstance(block, dict)
    return block


class TestPlacement:
    def test_replaces_the_commented_placeholder(self, tmp_path: Path) -> None:
        """The shipped `# slug:` line marks where the author meant it to go."""
        path = _user_config(tmp_path)
        assert "# slug: your-model-tag" in path.read_text(encoding="utf-8")

        save_tier_slug("local", "brain", _TAG, path)

        assert _tier_block(path, "local", "brain")["slug"] == _TAG
        # The placeholder was consumed, not left beside the real key.
        brain = path.read_text(encoding="utf-8").split("  local:")[1].split("worker:")[0]
        assert "your-model-tag" not in brain

    def test_overwrites_a_live_slug(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        save_tier_slug("local", "brain", _TAG, path)
        save_tier_slug("local", "brain", "gemma4:12b", path)

        assert _tier_block(path, "local", "brain")["slug"] == "gemma4:12b"
        assert path.read_text(encoding="utf-8").count(_TAG) == 0

    def test_writes_one_tier_without_touching_its_siblings(self, tmp_path: Path) -> None:
        """`local` points all three tiers at one endpoint; they stay separable."""
        path = _user_config(tmp_path)
        save_tier_slug("local", "worker", _TAG, path)

        assert _tier_block(path, "local", "worker")["slug"] == _TAG
        assert "slug" not in _tier_block(path, "local", "brain")
        assert "slug" not in _tier_block(path, "local", "validator")

    def test_inserts_when_no_slug_line_exists(self, tmp_path: Path) -> None:
        """A hand-written config with no placeholder still takes an edit.

        Loopback, because an off-box tier without a slug is already invalid —
        see ``save_tier_slug``: it loads before it writes, so it cannot
        repair a config the missing slug itself made unloadable.
        """
        path = tmp_path / "config.yaml"
        path.write_text(
            "active_preset: mine\n"
            "presets:\n"
            "  mine:\n"
            "    brain:\n"
            "      base_url: http://127.0.0.1:11434/v1\n"
            "      input_price: 1.0\n"
            "      output_price: 2.0\n"
            "      cache_read_price: 0.5\n"
            "      context_window: 1000\n"
            "      max_output_tokens: 100\n"
            "    worker:\n"
            "      base_url: http://127.0.0.1:11434/v1\n"
            "      input_price: 1.0\n"
            "      output_price: 2.0\n"
            "      cache_read_price: 0.5\n"
            "      context_window: 1000\n"
            "      max_output_tokens: 100\n"
            "    validator:\n"
            "      base_url: http://127.0.0.1:11434/v1\n"
            "      input_price: 1.0\n"
            "      output_price: 2.0\n"
            "      cache_read_price: 0.5\n"
            "      context_window: 1000\n"
            "      max_output_tokens: 100\n",
            encoding="utf-8",
        )
        save_tier_slug("mine", "brain", _TAG, path)

        assert _tier_block(path, "mine", "brain")["slug"] == _TAG
        assert _tier_block(path, "mine", "brain")["base_url"] == "http://127.0.0.1:11434/v1"


class TestWhatSurvives:
    def test_the_teaching_comments_survive(self, tmp_path: Path) -> None:
        """The reason this write is surgical instead of a YAML dump."""
        path = _user_config(tmp_path)
        before = path.read_text(encoding="utf-8")
        commentary = [ln for ln in before.split("\n") if ln.strip().startswith("#")]
        assert len(commentary) > 20, "fixture is meant to be a comment-heavy config"

        save_tier_slug("local", "brain", _TAG, path)

        after = path.read_text(encoding="utf-8").split("\n")
        # Every comment but the consumed placeholder is still there, verbatim.
        for line in commentary:
            if "your-model-tag" in line:
                continue
            assert line in after, f"lost a comment: {line!r}"

    def test_the_result_still_loads(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        save_tier_slug("local", "brain", _TAG, path)

        config = load_config(path)
        assert config.presets["local"].brain.slug == _TAG


class TestRefusals:
    def test_a_newline_cannot_inject_yaml(self, tmp_path: Path) -> None:
        """The value is data, not a fragment of the document it lands in."""
        path = _user_config(tmp_path)
        save_tier_slug("local", "brain", "evil\nactive_preset: budget", path)

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        # One scalar, and the injected key did not become a key.
        assert data["presets"]["local"]["brain"]["slug"] == "evil\nactive_preset: budget"
        assert data.get("active_preset") != "budget"

    @pytest.mark.parametrize("blank", ["", "   ", "\n"])
    def test_a_blank_slug_is_refused(self, tmp_path: Path, blank: str) -> None:
        """Blank is not "unset" — removing the key is how you get discovery."""
        path = _user_config(tmp_path)
        with pytest.raises(ConfigError, match="cannot be blank"):
            save_tier_slug("local", "brain", blank, path)

    def test_an_unknown_preset_is_refused(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        with pytest.raises(ConfigError, match="Unknown preset"):
            save_tier_slug("nope", "brain", _TAG, path)

    def test_an_unknown_tier_is_refused(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        with pytest.raises(ConfigError, match="Unknown tier"):
            save_tier_slug("local", "cortex", _TAG, path)

    def test_a_config_without_presets_is_refused(self, tmp_path: Path) -> None:
        """A shape this writer does not understand fails loudly, not silently."""
        path = tmp_path / "config.yaml"
        path.write_text("active_preset: mine\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            save_tier_slug("mine", "brain", _TAG, path)
