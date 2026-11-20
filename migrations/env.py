from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from prodata.config import get_settings
from prodata.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# PostGIS ships its own tables (spatial_ref_sys, tiger…); never touch them.
POSTGIS_TABLES = {"spatial_ref_sys", "geography_columns", "geometry_columns", "raster_columns", "raster_overviews"}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and reflected and compare_to is None:
        return False
    return not (type_ == "table" and name in POSTGIS_TABLES)


def url() -> str:
    return config.attributes.get("url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=url(), target_metadata=target_metadata, literal_binds=True, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection") or create_engine(url())
    if hasattr(connectable, "connect"):
        with connectable.connect() as connection:
            _run(connection)
    else:
        _run(connectable)


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
