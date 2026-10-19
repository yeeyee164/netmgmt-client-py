import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.schema_context import ContextRegistry, SchemaContext


def test_context_registry_caching_and_isolation(tmp_path):
    registry = ContextRegistry()

    dir_a = tmp_path / "models_a"
    dir_a.mkdir()
    dir_b = tmp_path / "models_b"
    dir_b.mkdir()

    ctx1 = registry.get_or_create_context(profile_id="profile-alpha", search_dirs=[dir_a])
    assert isinstance(ctx1, SchemaContext)
    assert ctx1.profile_id == "profile-alpha"

    # Same profile retrieves identical SchemaContext instance
    ctx1_again = registry.get_or_create_context(profile_id="profile-alpha")
    assert ctx1 is ctx1_again

    # Different profile creates distinct SchemaContext instance
    ctx2 = registry.get_or_create_context(profile_id="profile-beta", search_dirs=[dir_b])
    assert ctx2.profile_id == "profile-beta"
    assert ctx1 is not ctx2
    assert ctx1.yang is not ctx2.yang

