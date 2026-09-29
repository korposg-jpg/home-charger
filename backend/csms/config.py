from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CSMS_", env_file=".env", extra="ignore")

    # SQLite database file. In the container this lives on a mounted volume.
    db_path: str = "data/csms.db"

    host: str = "0.0.0.0"
    port: int = 9000

    # Comma-separated charge point identities allowed to connect. Empty means any
    # identity may connect (handy on install day, before the real id is known).
    allowed_charge_points: str = ""

    # Optional OCPP security profile 1 (HTTP Basic auth). When set, the charger must
    # send "<chargePointId>:<password>" as Basic credentials.
    ocpp_basic_auth_password: str = ""

    # Accept RFID tags that aren't registered to a user. Off by default; rejected
    # tags are still logged so a new card's id can be found and registered.
    accept_unknown_id_tags: bool = False

    heartbeat_interval: int = 300
    # How long to wait for the charger to answer a request we send it.
    ocpp_response_timeout: int = 30

    # App login tokens expire after this many days of not being used.
    token_ttl_days: int = 90

    # Keep this many days of raw OCPP message log.
    ocpp_log_retention_days: int = 30

    def allowed_ids(self) -> set[str]:
        return {x.strip() for x in self.allowed_charge_points.split(",") if x.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
