from typing import Any, Optional
from supabase import create_client, Client
from app.core.config import settings

_client: Optional[Client] = None


def get_supabase() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    return _client


def call_rpc(function_name: str, params: dict[str, Any]) -> Any:
    response = get_supabase().rpc(function_name, params).execute()
    return response.data


def first_row(data: Any) -> Any:
    if isinstance(data, list):
        return data[0] if data else None
    return data
