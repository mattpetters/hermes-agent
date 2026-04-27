"""Google Gemini provider profiles.

gemini:            Google AI Studio (API key) — uses GeminiNativeClient
google-gemini-cli: Google Cloud Code Assist (OAuth) — uses GeminiCloudCodeClient

Both report api_mode="chat_completions" but use custom native clients
that bypass the standard OpenAI transport. The profile captures auth
and endpoint metadata for auth.py / runtime_provider.py migration.
"""

from providers import register_provider
from providers.base import ProviderProfile

gemini = ProviderProfile(
    name="gemini",
    aliases=("google", "google-gemini", "google-ai-studio"),
    api_mode="chat_completions",
    env_vars=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    base_url="https://generativelanguage.googleapis.com/v1beta",
    auth_type="api_key",
    default_aux_model="gemini-3-flash-preview",
)

google_gemini_cli = ProviderProfile(
    name="google-gemini-cli",
    aliases=("gemini-cli", "gemini-oauth"),
    api_mode="chat_completions",
    env_vars=(),  # OAuth — no API key
    base_url="cloudcode-pa://google",  # Cloud Code Assist internal scheme
    auth_type="oauth_external",
)

register_provider(gemini)
register_provider(google_gemini_cli)
