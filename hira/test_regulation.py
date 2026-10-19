from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from audit.models import AuditLog
from projects.models import Project, ProjectMember
from users.models import User
from .models import HIRAActivity, HIRADocument, HIRAGroup

R = User.Role
URL = '/api/hira/documents/'


def act(name, order=1, **over):
    data = {
        'order': order, 'activity_name': name, 'activity_type': 'R',
        'potential_hazard': 'x', 'risk_impact': 'y',
        'initial_severity': 4, 'initial_likelihood': 3,       # Rt 12
        'residual_severity': 4, 'residual_likelihood': 2,     # Rt 8: above the limit, still recordable
    }
    data.update(over)
    return data


class GroupRegulationTests(TestCase):
    REGS = '- UU No.1 Tahun 1970\n- Permenaker No.9 Tahun 2016'

    def setUp(self):
        mk = lambda n, r: User.objects.create_user(f'{n}@t.com', 'pass12345', full_name=n, role=r)
        self.p1 = Project.objects.create(name='P1', location='L', client_name='C', start_date='2026-10-01')
        self.sub, self.officer, self.pm = mk('sub', R.SUBCONTRACTOR), mk('off', R.HSE_OFFICER), mk('pm', R.PROJECT_MANAGER)
        for u in (self.sub, self.pm):
            ProjectMember.objects.create(project=self.p1, user=u)

    def call(self, user, method, url, data=None):
        c = APIClient()
        c.force_authenticate(user)
        return getattr(c, method)(url, data or {}, format='json')

    def create(self, **group_over):
        group = {'location_function': 'Rangka Atap', 'applicable_regulation': self.REGS,
                 'activities': [act('A'), act('B', 2)]}
        group.update(group_over)
        r = self.call(self.sub, 'post', URL, {'project': self.p1.id, 'work_package': 'WP',
                                              'work_area': 'WA', 'groups': [group]})
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_regulations_are_stored_on_the_group_and_activities_read_them_from_it(self):
        data = self.create()
        group = data['groups'][0]
        self.assertEqual(group['applicable_regulation'], self.REGS)
        self.assertEqual([a['applicable_regulation'] for a in group['activities']], [self.REGS] * 2)
        self.assertEqual(HIRAGroup.objects.get().applicable_regulation, self.REGS)
        self.assertFalse(hasattr(HIRAActivity, 'applicable_regulation'))

    def test_an_activity_cannot_carry_its_own_regulation(self):
        data = self.create(activities=[act('A', applicable_regulation='- Something else')])
        self.assertEqual(data['groups'][0]['activities'][0]['applicable_regulation'], self.REGS)
        pk = data['groups'][0]['activities'][0]['id']
        r = self.call(self.sub, 'patch', f'/api/hira/activities/{pk}/',
                      {'applicable_regulation': '- Sneaky', 'activity_name': 'Renamed'})
        self.assertEqual((r.status_code, r.data['activity_name']), (200, 'Renamed'))
        self.assertEqual(r.data['applicable_regulation'], self.REGS)

    def test_editing_the_group_changes_what_every_activity_shows(self):
        data = self.create()
        gid = data['groups'][0]['id']
        r = self.call(self.sub, 'patch', f'/api/hira/groups/{gid}/', {'applicable_regulation': '- Baru'})
        self.assertEqual(r.status_code, 200, r.data)
        detail = self.call(self.sub, 'get', f'{URL}{data["id"]}/').data
        self.assertEqual([a['applicable_regulation'] for a in detail['groups'][0]['activities']], ['- Baru'] * 2)
        listing = self.call(self.sub, 'get', f'/api/hira/activities/?hira_document={data["id"]}').data
        self.assertEqual({a['applicable_regulation'] for a in listing}, {'- Baru'})
        entry = AuditLog.objects.filter(resource_type='hira.hiragroup', resource_id=gid, action='updated').get()
        self.assertEqual(entry.snapshot['applicable_regulation'], '- Baru')

    def test_a_risk_above_the_limit_is_recorded_and_the_history_keeps_it(self):
        """Residual Rt above 4 can be saved and submitted but never approved. When the author
        later lowers it after a rejection, the audit log still holds the original assessment."""
        data = self.create()
        doc, activity = data['id'], data['groups'][0]['activities'][0]
        self.assertEqual(activity['residual_score'], 8)
        self.assertTrue(self.call(self.sub, 'get', f'{URL}{doc}/').data['has_unacceptable_residual'])
        self.assertEqual(self.call(self.sub, 'post', f'{URL}{doc}/submit/').status_code, 200)
        self.assertEqual(self.call(self.officer, 'post', f'{URL}{doc}/approve/').status_code, 400)
        self.call(self.officer, 'post', f'{URL}{doc}/reject/', {'remarks': 'Residual terlalu tinggi'})
        r = self.call(self.sub, 'patch', f'/api/hira/activities/{activity["id"]}/',
                      {'residual_severity': 2, 'residual_likelihood': 2})
        self.assertEqual(r.data['residual_score'], 4)
        scores = list(AuditLog.objects.filter(resource_type='hira.hiraactivity', resource_id=activity['id'])
                      .order_by('id').values_list('action', 'snapshot__residual_score'))
        self.assertEqual(scores, [('created', 8), ('updated', 4)])   # the 8 is still on record


class RegulationMigrationTests(TransactionTestCase):
    """0004 moves each activity's regulations onto its group (distinct lines, in order)."""
    before = [('hira', '0003_remove_hiradocument_external_procedure_url_and_more')]
    after = [('hira', '0005_remove_hiraactivity_applicable_regulation')]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def test_regulations_move_to_the_group_and_back(self):
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(
            MigrationExecutor(connection).loader.graph.leaf_nodes()))
        old = self.migrate(self.before)
        user = old.get_model('users', 'User').objects.create(email='m@t.com', full_name='m', role='subcontractor')
        project = old.get_model('projects', 'Project').objects.create(
            name='P', location='L', client_name='C', start_date='2026-10-01')
        doc = old.get_model('hira', 'HIRADocument').objects.create(
            project=project, work_package='WP', work_area='WA', created_by=user)
        group = old.get_model('hira', 'HIRAGroup').objects.create(hira_document=doc, location_function='G')
        empty = old.get_model('hira', 'HIRAGroup').objects.create(hira_document=doc, location_function='E')
        scores = dict(initial_severity=1, initial_likelihood=1, initial_score=1, initial_category='R',
                      initial_acceptable=True, residual_severity=1, residual_likelihood=1,
                      residual_score=1, residual_category='R', residual_acceptable=True)
        Activity = old.get_model('hira', 'HIRAActivity')
        for order, regs in ((1, '- A\n- B'), (2, '- B\n- C\n'), (3, '')):
            Activity.objects.create(group=group, order=order, activity_name=f'a{order}', activity_type='R',
                                    potential_hazard='x', risk_impact='y', applicable_regulation=regs, **scores)

        new = self.migrate(self.after)
        Group = new.get_model('hira', 'HIRAGroup')
        self.assertEqual(Group.objects.get(pk=group.pk).applicable_regulation, '- A\n- B\n- C')
        self.assertEqual(Group.objects.get(pk=empty.pk).applicable_regulation, '')

        back = self.migrate(self.before)
        regs = set(back.get_model('hira', 'HIRAActivity').objects.values_list('applicable_regulation', flat=True))
        self.assertEqual(regs, {'- A\n- B\n- C'})   # every activity of the group gets the merged text
