"""Runs only via moneta.db.init_db, which injects a live connection.

New revisions are written by hand in versions/ (id NNNN, down_revision = previous).
"""

from alembic import context
from sqlalchemy.engine import Connection

from moneta.models import Base

connection: Connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
