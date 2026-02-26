# Assistant module

Required for the API server. Provides `build_context`, `get_suggestions`, `validate_suggested_actions`, and `SuggestedAction` for the `/api/v1/assistant/recommend` and `/api/v1/assistant/apply` endpoints.

Without this package, `from src.assistant import ...` fails and the server cannot start.
