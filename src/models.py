from dataclasses import dataclass

@dataclass
class ProviderStatus:
    ok: bool
    message: str
