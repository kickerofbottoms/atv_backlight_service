import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import ClassVar

from dacite import from_dict


@dataclass
class AppleTVConfig:
    name: str
    light_id: int | None = None


@dataclass
class HueConfig:
    address: str
    token: str


@dataclass
class Config:
    atv: dict[str, AppleTVConfig] = field(default_factory=dict)
    hue: HueConfig | None = None

    bridge_username: str = "atv_backlight_service"  # to be hashed during reg
    backlight_transition: int = 10  # in deciseconds

    debug: bool = False

    _config_path: ClassVar[Path] = Path.home() / ".atvbacklight.json"

    @classmethod
    def load(cls) -> "Config":
        try:
            with cls._config_path.open() as f:
                data = json.load(f)
        except FileNotFoundError:
            return cls()

        return from_dict(data_class=cls, data=data)

    def save(self) -> None:
        with self._config_path.open("w") as f:
            json.dump(asdict(self), f, indent=4)
            f.write("\n")
