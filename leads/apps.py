from django.apps import AppConfig
from django.db.backends.signals import connection_created

def tune_sqlite(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")

class LeadsConfig(AppConfig):
    name = "leads"
    def ready(self):
        connection_created.connect(tune_sqlite, dispatch_uid="leads_sqlite_wal")
