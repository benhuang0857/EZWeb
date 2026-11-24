from pydantic import BaseModel


class SiteCreate(BaseModel):
    display_name: str
    template_id: str
    wp_title: str
    wp_admin_user: str
    wp_admin_pass: str
    wp_admin_email: str
    domain_develop: str | None = None
    domain_qa: str | None = None
    domain_prod: str | None = None
    stage: str = "develop"
    wp_port: int | None = None
    pma_port: int | None = None


class StageUpdate(BaseModel):
    stage: str


class DomainsUpdate(BaseModel):
    domain_develop: str | None = None
    domain_qa: str | None = None
    domain_prod: str | None = None


class WpCliCommand(BaseModel):
    args: list[str]


class CleanupExecuteRequest(BaseModel):
    # None = all categories. See app/sites/service.py::_CLEANUP_CATEGORIES.
    categories: list[str] | None = None


class BackupSchedule(BaseModel):
    # off / daily / weekly. hour = hour of day (0-23, server local time).
    # keep = how many automatic backups to retain.
    schedule: str
    hour: int = 3
    keep: int = 5
