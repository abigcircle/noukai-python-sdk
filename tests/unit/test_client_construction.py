"""Phase 4: Client construction, env-var resolution, flow arg parsing."""

import pytest

from noukai_sdk import AsyncNoukai, AuthenticationError, Noukai


class TestApiKeyResolution:
    def test_explicit_key_wins(self, monkeypatch):
        monkeypatch.setenv("NOUKAI_API_KEY", "nk_env")
        client = Noukai(api_key="nk_explicit")
        assert client._transport._api_key == "nk_explicit"
        client.close()

    def test_env_var_fallback(self, monkeypatch):
        monkeypatch.setenv("NOUKAI_API_KEY", "nk_env123")
        client = Noukai()
        assert client._transport._api_key == "nk_env123"
        client.close()

    def test_no_key_raises_auth_error(self, monkeypatch):
        monkeypatch.delenv("NOUKAI_API_KEY", raising=False)
        with pytest.raises(AuthenticationError):
            Noukai()

    def test_wrong_prefix_raises_auth_error(self):
        with pytest.raises(AuthenticationError):
            Noukai(api_key="sk_wrong_prefix")


class TestBaseUrl:
    def test_default_base_url(self, monkeypatch):
        monkeypatch.delenv("NOUKAI_ENV", raising=False)
        client = Noukai(api_key="nk_x")
        assert client._transport._base_url.startswith("https://api.noukai.xyz/api/v1")
        client.close()

    def test_env_dev_points_at_localhost(self):
        client = Noukai(api_key="nk_x", env="dev")
        assert client._transport._base_url.startswith("http://localhost:8080/api/v1")
        client.close()

    def test_env_production_uses_production_url(self, monkeypatch):
        monkeypatch.delenv("NOUKAI_ENV", raising=False)
        client = Noukai(api_key="nk_x", env="production")
        assert client._transport._base_url.startswith("https://api.noukai.xyz/api/v1")
        client.close()

    def test_noukai_env_var_dev(self, monkeypatch):
        monkeypatch.setenv("NOUKAI_ENV", "dev")
        client = Noukai(api_key="nk_x")
        assert client._transport._base_url.startswith("http://localhost:8080/api/v1")
        client.close()

    def test_noukai_env_var_development_alias(self, monkeypatch):
        monkeypatch.setenv("NOUKAI_ENV", "development")
        client = Noukai(api_key="nk_x")
        assert client._transport._base_url.startswith("http://localhost:8080/api/v1")
        client.close()

    def test_env_option_wins_over_noukai_env_var(self, monkeypatch):
        monkeypatch.setenv("NOUKAI_ENV", "production")
        client = Noukai(api_key="nk_x", env="dev")
        assert client._transport._base_url.startswith("http://localhost:8080/api/v1")
        client.close()

    def test_noukai_base_url_env_var_ignored(self, monkeypatch):
        """NOUKAI_BASE_URL is no longer respected — no env-var escape hatch."""
        monkeypatch.setenv("NOUKAI_BASE_URL", "https://attacker.example.com/api/v1")
        monkeypatch.delenv("NOUKAI_ENV", raising=False)
        client = Noukai(api_key="nk_x")
        assert client._transport._base_url.startswith("https://api.noukai.xyz/api/v1")
        client.close()


class TestContextManager:
    def test_sync_close_releases_transport(self):
        with Noukai(api_key="nk_x") as client:
            assert client._transport is not None
        # After context exit transport is closed; subsequent ops would error.

    @pytest.mark.asyncio
    async def test_async_aclose(self):
        async with AsyncNoukai(api_key="nk_x") as client:
            assert client._transport is not None


class TestFlowConstruction:
    def test_string_form_three_parts(self):
        client = Noukai(api_key="nk_x")
        flow = client.flow("acme/spelling/grade-3")
        assert flow.org == "acme"
        assert flow.project == "spelling"
        assert flow.slug == "grade-3"
        client.close()

    def test_kwargs_form(self):
        client = Noukai(api_key="nk_x")
        flow = client.flow(org="acme", project="spelling", slug="grade-3")
        assert flow.org == "acme"
        client.close()

    def test_string_form_too_few_parts_raises(self):
        client = Noukai(api_key="nk_x")
        with pytest.raises(ValueError, match="org/project/slug"):
            client.flow("acme/spelling")
        client.close()

    def test_string_form_too_many_parts_raises(self):
        client = Noukai(api_key="nk_x")
        with pytest.raises(ValueError, match="org/project/slug"):
            client.flow("acme/spelling/grade-3/extra")
        client.close()

    def test_kwargs_partial_raises(self):
        client = Noukai(api_key="nk_x")
        with pytest.raises(ValueError, match="Kwargs form requires"):
            client.flow(org="acme", project="spelling")  # no slug
        client.close()

    def test_string_and_kwargs_both_raises(self):
        client = Noukai(api_key="nk_x")
        with pytest.raises(ValueError, match="either"):
            client.flow("a/b/c", org="acme")
        client.close()

    def test_single_segment_without_defaults_raises(self):
        client = Noukai(api_key="nk_x")
        with pytest.raises(ValueError, match="constructed with org and project"):
            client.flow("grade-3")
        client.close()


class TestClientLevelDefaults:
    def test_single_segment_uses_defaults(self):
        client = Noukai(api_key="nk_x", org="abc", project="nouko")
        flow = client.flow("language-analysis")
        assert flow.org == "abc"
        assert flow.project == "nouko"
        assert flow.slug == "language-analysis"
        client.close()

    def test_three_segment_overrides_defaults(self):
        client = Noukai(api_key="nk_x", org="abc", project="nouko")
        flow = client.flow("other-org/other-proj/other-slug")
        assert flow.org == "other-org"
        assert flow.project == "other-proj"
        assert flow.slug == "other-slug"
        client.close()

    def test_kwargs_form_overrides_defaults(self):
        client = Noukai(api_key="nk_x", org="abc", project="nouko")
        flow = client.flow(org="other", project="other-proj", slug="other-slug")
        assert flow.org == "other"
        client.close()

    def test_org_without_project_raises(self):
        with pytest.raises(ValueError, match="together"):
            Noukai(api_key="nk_x", org="abc")

    def test_project_without_org_raises(self):
        with pytest.raises(ValueError, match="together"):
            Noukai(api_key="nk_x", project="nouko")

    def test_default_org_project_attributes(self):
        client = Noukai(api_key="nk_x", org="abc", project="nouko")
        assert client.default_org == "abc"
        assert client.default_project == "nouko"
        client.close()

    def test_defaults_none_when_not_provided(self):
        client = Noukai(api_key="nk_x")
        assert client.default_org is None
        assert client.default_project is None
        client.close()


class TestAsyncClientLevelDefaults:
    async def test_async_single_segment_uses_defaults(self):
        from noukai_sdk import AsyncNoukai

        client = AsyncNoukai(api_key="nk_x", org="abc", project="nouko")
        flow = client.flow("language-analysis")
        assert flow.org == "abc"
        assert flow.project == "nouko"
        assert flow.slug == "language-analysis"
        await client.aclose()

    async def test_async_org_without_project_raises(self):
        from noukai_sdk import AsyncNoukai

        with pytest.raises(ValueError, match="together"):
            AsyncNoukai(api_key="nk_x", org="abc")
