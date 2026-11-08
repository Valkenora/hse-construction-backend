from django.test import TestCase


class HealthzTests(TestCase):
    def test_answers_without_login_and_without_touching_the_database(self):
        with self.assertNumQueries(0):
            r = self.client.get('/healthz/')
        self.assertEqual((r.status_code, r.json()), (200, {'status': 'ok'}))

    def test_head_works_and_writes_are_refused(self):
        self.assertEqual(self.client.head('/healthz/').status_code, 200)
        self.assertEqual(self.client.post('/healthz/').status_code, 405)
