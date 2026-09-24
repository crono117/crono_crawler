from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE, AFTER = [('leads', '0002_source_allow_homepage_source_approval_kind_and_more')], [('leads', '0003_lead_status_origin')]


class StatusOriginBackfillTests(TransactionTestCase):
    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def tearDown(self):
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def test_existing_decisions_are_operator_and_existing_holds_cover_namesakes(self):
        apps = self.migrate(BEFORE)
        Source, Lead, Observation = (apps.get_model('leads', name) for name in ('Source', 'Lead', 'Observation'))
        source = Source.objects.create(name='Migration fixture', company='Example Payments', approved=True,
                                       url='https://migrate.example.test/team/', allowed_paths='/team/')

        def lead(key, name, status, url=source.url):
            row = Lead.objects.create(identity=key, name=name, status=status)
            Observation.objects.create(lead=row, source=source, page_url=url, source_category=source.category,
                                       evidence=name, content_hash='x')
            return row.pk

        held = lead('a', 'Alex Example', 'suppressed')
        earlier = lead('b', 'Alex Example', 'new')
        reviewed = lead('c', 'Alex Example', 'reviewed')
        other_name = lead('d', 'Jordan Sample', 'new')
        other_page = lead('e', 'Alex Example', 'new', url='https://migrate.example.test/team/other/')

        Lead = self.migrate(AFTER).get_model('leads', 'Lead')
        state = {row.pk: (row.status, row.status_origin, row.held_by_id) for row in Lead.objects.all()}
        self.assertEqual(state[held], ('suppressed', 'operator', None))
        self.assertEqual(state[earlier], ('suppressed', 'hold', held))
        self.assertEqual(state[reviewed], ('reviewed', 'operator', None))
        self.assertEqual(state[other_name], ('new', 'default', None))
        self.assertEqual(state[other_page], ('new', 'default', None))
        self.assertIn(f'lead #{held} ', Lead.objects.get(pk=earlier).notes)
