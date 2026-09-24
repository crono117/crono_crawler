import csv
import io

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from leads.models import Lead, Observation, Source
from leads.services.extraction import extract
from leads.services.storage import save_records

# Before extraction 0.4.0, the default recipe read only mailto:/tel: links, so this card
# was stored phone-only under the page-scoped identity. An explicit recipe that selects
# no email reproduces that old record from the same HTML.
OLD_FORMAT = {'email': 'a.no-email-selected'}


def card(name, body):
    return f'<div class="team-member"><h3>{name}</h3><p class="role">Sales Manager</p>{body}</div>'


def page(*cards):
    return f'<html><body><h1>Example Payments</h1>{"".join(cards)}</body></html>'


ENRICHED = card('Alex Example', '<a href="tel:+15550101234">Direct line</a><span class="email">alex@example.test</span>')


class StorageFixture(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name='Continuity fixture', company='Example Payments', approved=True,
                                            url='https://continuity.example.test/team/', allowed_paths='/team/')
        self.staff = get_user_model().objects.create_user('operator', password='Test-only-long-password-123',
                                                          is_staff=True)

    def crawl(self, html, recipe=None):
        self.source.recipe = recipe or {}
        records, tags = extract(html, self.source)
        save_records(self.source, self.source.url, 'same-body', records, tags)
        return records

    def export_rows(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('export'))
        return list(csv.DictReader(io.StringIO(response.content.decode())))


class ContactContinuityTests(StorageFixture):
    def seed_old_record(self, html=None, status='new', notes=''):
        records = self.crawl(html or page(ENRICHED), OLD_FORMAT)
        self.assertEqual([(r['name'], r['email'], r['phone']) for r in records],
                         [('Alex Example', '', '+15550101234')])
        lead = Lead.objects.get()
        lead.status, lead.notes = status, notes
        lead.save(update_fields=['status', 'notes'])
        return lead

    def test_suppressed_lead_keeps_status_and_notes_when_email_is_added(self):
        lead = self.seed_old_record(status='suppressed', notes='Do not contact: operator decision.')

        records = self.crawl(page(ENRICHED))

        self.assertEqual(records[0]['email'], 'alex@example.test')
        stored = Lead.objects.get()
        self.assertEqual(stored.pk, lead.pk)
        self.assertEqual((stored.email, stored.phone, stored.status, stored.notes),
                         ('alex@example.test', '+15550101234', 'suppressed', 'Do not contact: operator decision.'))
        self.assertEqual(self.export_rows(), [])

    def test_rejected_lead_stays_rejected_and_out_of_export(self):
        lead = self.seed_old_record(status='rejected', notes='Not a sales role.')

        self.crawl(page(ENRICHED))

        stored = Lead.objects.get()
        self.assertEqual((stored.pk, stored.status, stored.notes), (lead.pk, 'rejected', 'Not a sales role.'))
        self.assertEqual(self.export_rows(), [])

    def test_reviewed_notes_survive_enrichment_and_export_once(self):
        lead = self.seed_old_record(status='reviewed', notes='Met at trade show.')

        self.crawl(page(ENRICHED))

        rows = self.export_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['email'], rows[0]['review_status'], rows[0]['notes']),
                         ('alex@example.test', 'reviewed', 'Met at trade show.'))
        self.assertEqual(Lead.objects.get().pk, lead.pk)

    def test_subsequent_crawl_after_enrichment_reuses_the_same_lead(self):
        lead = self.seed_old_record(status='suppressed', notes='Keep suppressed.')
        self.crawl(page(ENRICHED))

        self.crawl(page(ENRICHED))

        self.assertEqual(list(Lead.objects.values_list('pk', 'status')), [(lead.pk, 'suppressed')])
        self.assertEqual(Observation.objects.filter(present=True).count(), 1)
        self.assertEqual(self.export_rows(), [])

    def test_conflicting_phone_is_not_merged_but_is_held_out_of_export(self):
        old = self.seed_old_record(status='suppressed', notes='Do not contact.')
        changed = card('Alex Example', '<a href="tel:+15550109999">Direct line</a><span class="email">alex@example.test</span>')

        self.crawl(page(changed))

        self.assertEqual(Lead.objects.count(), 2)
        successor = Lead.objects.exclude(pk=old.pk).get()
        self.assertEqual(successor.status, 'suppressed')
        self.assertIn(f'lead #{old.pk} ', successor.notes)
        self.assertEqual(Lead.objects.get(pk=old.pk).notes, 'Do not contact.')
        self.assertEqual(self.export_rows(), [])

    def test_hold_note_is_not_repeated_on_later_crawls(self):
        old = self.seed_old_record(status='suppressed')
        changed = page(card('Alex Example', '<a href="tel:+15550109999">Call</a><span class="email">alex@example.test</span>'))
        self.crawl(changed)
        successor = Lead.objects.exclude(pk=old.pk).get()
        successor.status = 'new'
        successor.save(update_fields=['status'])

        self.crawl(changed)

        successor.refresh_from_db()
        self.assertEqual(successor.notes.count(f'lead #{old.pk} '), 1)

    def test_two_cards_with_the_same_name_are_not_merged_into_the_old_lead(self):
        twins = page(card('Alex Example', '<a href="tel:+15550101234">Call</a><span class="email">alex@example.test</span>'),
                     card('Alex Example', '<a href="tel:+15550105555">Call</a><span class="email">alex.e@example.test</span>'))
        old = self.seed_old_record(html=page(card('Alex Example', '<a href="tel:+15550101234">Call</a>')),
                                   status='suppressed')

        self.crawl(twins)

        self.assertEqual(Lead.objects.get(pk=old.pk).email, '')
        successors = Lead.objects.exclude(pk=old.pk)
        self.assertEqual(successors.count(), 2)
        self.assertEqual(set(successors.values_list('status', flat=True)), {'suppressed'})
        self.assertEqual(self.export_rows(), [])

    def test_unreviewed_old_lead_is_continued_without_a_duplicate(self):
        lead = self.seed_old_record()

        self.crawl(page(ENRICHED))

        self.assertEqual(list(Lead.objects.values_list('pk', 'email', 'status')),
                         [(lead.pk, 'alex@example.test', 'new')])

    def test_different_people_sharing_a_phone_are_never_merged(self):
        html = page(card('Alex Example', '<a href="tel:+15550101000">Call</a><a href="mailto:alex@example.test">Email</a>'),
                    card('Jordan Sample', '<a href="tel:+15550101000">Call</a><a href="mailto:jordan@example.test">Email</a>'))
        self.crawl(html)

        self.assertEqual(sorted(Lead.objects.values_list('name', flat=True)), ['Alex Example', 'Jordan Sample'])

    def test_generic_inbox_keeps_page_scoped_identity(self):
        lead = self.seed_old_record(html=page(card('Alex Example', '<a href="tel:+15550101234">Call</a>')),
                                    status='suppressed')
        self.crawl(page(card('Alex Example', '<a href="tel:+15550101234">Call</a>'
                                             '<a href="mailto:info@example.test">Email</a>')))

        self.assertEqual(list(Lead.objects.values_list('pk', 'status')), [(lead.pk, 'suppressed')])

    def test_observation_records_contact_provenance(self):
        self.crawl(page(ENRICHED))

        facts = Observation.objects.get().facts
        self.assertEqual(facts['contact_provenance'], {'email': 'visible', 'phone': 'selector'})


PHONE = '<a href="tel:+15550101234">Direct line</a>'


def alex(body, place='West Coast sales'):
    return card('Alex Example', f'<p>{place}</p>{body}')


LATER_CRAWLS = {
    'email lost': page(alex(PHONE)),
    'email changed': page(alex(PHONE + '<span class="email">alex.new@example.test</span>')),
    'email ambiguous': page(alex(PHONE + '<span class="email">alex@example.test assistant@example.test</span>')),
}


class HeldDecisionAcrossContactChangesTests(StorageFixture):
    """A suppressed or rejected person stays out of export through every later contact change."""

    def seed_enriched(self, status):
        self.crawl(page(alex(PHONE + '<span class="email">alex@example.test</span>')), OLD_FORMAT)
        lead = Lead.objects.get()
        lead.status, lead.notes = status, 'Operator decision.'
        lead.save(update_fields=['status', 'notes'])
        self.crawl(page(alex(PHONE + '<span class="email">alex@example.test</span>')))
        self.assertEqual(list(Lead.objects.values_list('pk', 'email', 'status')),
                         [(lead.pk, 'alex@example.test', status)])
        self.assertEqual(self.export_rows(), [])
        return lead

    def test_later_contact_changes_never_export_a_held_person(self):
        for status in ('suppressed', 'rejected'):
            for label, html in LATER_CRAWLS.items():
                with self.subTest(status=status, change=label):
                    Lead.objects.all().delete()
                    original = self.seed_enriched(status)

                    self.crawl(html)
                    self.crawl(html)

                    original.refresh_from_db()
                    self.assertEqual((original.status, original.notes), (status, 'Operator decision.'))
                    successor = Lead.objects.exclude(pk=original.pk).get()
                    self.assertEqual(successor.status, status)
                    self.assertEqual(successor.notes.count(f'lead #{original.pk} '), 1)
                    self.assertEqual(self.export_rows(), [])

    def test_return_to_the_original_email_reuses_the_original_lead(self):
        for status in ('suppressed', 'rejected'):
            with self.subTest(status=status):
                Lead.objects.all().delete()
                original = self.seed_enriched(status)
                self.crawl(LATER_CRAWLS['email changed'])

                self.crawl(page(alex(PHONE + '<span class="email">alex@example.test</span>')))
                self.crawl(LATER_CRAWLS['email lost'])

                self.assertEqual(Observation.objects.get(present=True).lead.status, status)
                self.assertEqual(Lead.objects.get(pk=original.pk).status, status)
                self.assertEqual(set(Lead.objects.values_list('status', flat=True)), {status})
                self.assertEqual(self.export_rows(), [])

    def test_hold_follows_a_person_first_seen_with_an_email(self):
        self.crawl(page(alex(PHONE + '<a href="mailto:alex@example.test">Email</a>')))
        Lead.objects.update(status='suppressed')

        self.crawl(page(alex(PHONE)))

        self.assertEqual(set(Lead.objects.values_list('status', flat=True)), {'suppressed'})
        self.assertEqual(self.export_rows(), [])

    def test_unrelated_people_on_the_page_are_not_held(self):
        self.seed_enriched('suppressed')

        self.crawl(page(alex(PHONE), card('Jordan Sample', '<a href="mailto:jordan@example.test">Email</a>')))

        rows = self.export_rows()
        self.assertEqual([(r['name'], r['review_status']) for r in rows], [('Jordan Sample', 'new')])

    def test_operator_can_release_a_held_successor(self):
        self.seed_enriched('suppressed')
        self.crawl(LATER_CRAWLS['email changed'])
        successor = Lead.objects.get(email='alex.new@example.test')
        successor.status = 'reviewed'
        successor.save(update_fields=['status'])

        self.crawl(LATER_CRAWLS['email changed'])

        self.assertEqual([r['email'] for r in self.export_rows()], ['alex.new@example.test'])


class AmbiguousSameNameCardTests(StorageFixture):
    TWO_CARDS = page(alex('', 'West Coast sales'),
                     alex('<span class="email">alex.other@example.test</span>', 'East Coast sales'))

    def seed(self, status, notes):
        self.crawl(page(alex(PHONE)))
        lead = Lead.objects.get()
        lead.status, lead.notes = status, notes
        lead.save(update_fields=['status', 'notes'])
        return lead

    def test_card_count_is_taken_before_contact_filtering(self):
        records = self.crawl(self.TWO_CARDS)

        self.assertEqual([(r['email'], r['name_cards']) for r in records], [('alex.other@example.test', 2)])

    def test_one_surviving_record_does_not_prove_continuity(self):
        original = self.seed('reviewed', 'Original Alex: verified at an event.')

        self.crawl(self.TWO_CARDS)

        original.refresh_from_db()
        self.assertEqual((original.email, original.phone, original.status, original.notes),
                         ('', '+15550101234', 'reviewed', 'Original Alex: verified at an event.'))
        other = Lead.objects.exclude(pk=original.pk).get()
        self.assertEqual((other.email, other.phone, other.status, other.notes),
                         ('alex.other@example.test', '', 'new', ''))
        rows = {r['email']: r for r in self.export_rows()}
        self.assertEqual(rows['alex.other@example.test']['review_status'], 'new')
        self.assertEqual(rows['alex.other@example.test']['notes'], '')
        self.assertEqual(rows['']['notes'], 'Original Alex: verified at an event.')

    def test_ambiguous_namesake_of_a_suppressed_lead_is_held(self):
        original = self.seed('suppressed', 'Do not contact.')

        self.crawl(self.TWO_CARDS)

        self.assertEqual(Lead.objects.get(pk=original.pk).email, '')
        self.assertEqual(set(Lead.objects.values_list('status', flat=True)), {'suppressed'})
        self.assertEqual(self.export_rows(), [])

    def test_unique_card_still_enriches_the_reviewed_lead(self):
        original = self.seed('reviewed', 'Original Alex: verified at an event.')

        self.crawl(page(alex(PHONE + '<span class="email">alex@example.test</span>')))

        rows = self.export_rows()
        self.assertEqual([(r['email'], r['review_status'], r['notes']) for r in rows],
                         [('alex@example.test', 'reviewed', 'Original Alex: verified at an event.')])
        self.assertEqual(Lead.objects.get().pk, original.pk)

    def test_records_without_a_card_count_never_prove_continuity(self):
        original = self.seed('suppressed', 'Do not contact.')
        [record] = extract(page(alex(PHONE + '<a href="mailto:alex@example.test">Email</a>')), self.source)[0]
        del record['name_cards']

        save_records(self.source, self.source.url, 'ai-body', [record], [])

        self.assertEqual(Lead.objects.get(pk=original.pk).email, '')
        self.assertEqual(set(Lead.objects.values_list('status', flat=True)), {'suppressed'})
