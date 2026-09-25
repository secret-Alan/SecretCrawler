"""Small, explicit client for the local SecretCrawler approval API."""
from dataclasses import dataclass
from typing import Literal
import requests

Role = Literal["owner", "operator", "viewer"]
Action = Literal["create_task", "start_download", "join_owned_room", "read_status", "change_network_policy", "export_data"]

@dataclass(frozen=True)
class SecretCrawlerClient:
    base_url: str = "http://127.0.0.1:8080"
    timeout_seconds: float = 10
    def health(self) -> dict:
        return requests.get(f"{self.base_url}/health", timeout=self.timeout_seconds).json()
    def request_approval(self, *, role: Role, action: Action, actor: str, justification: str) -> dict:
        """Submit a reviewable request; this method never executes the action."""
        response = requests.post(f"{self.base_url}/v1/approvals", json={"role": role, "action": action, "actor": actor, "justification": justification}, timeout=self.timeout_seconds)
        response.raise_for_status()
        return response.json()
