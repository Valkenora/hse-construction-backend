from django.test import TestCase
from rest_framework.test import APIClient

from .models import User


class AuthTests(TestCase):
    def setUp(self):
        User.objects.create_user('a@t.com', 'pass12345', full_name='A', role=User.Role.HSE_OFFICER)

    def test_login_refresh_me(self):
        c = APIClient()
        r = c.post('/api/users/login/', {'email': 'a@t.com', 'password': 'pass12345'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.data), {'access', 'refresh', 'user'})

        r2 = c.post('/api/users/token/refresh/', {'refresh': r.data['refresh']}, format='json')
        self.assertEqual(r2.status_code, 200)
        self.assertIn('access', r2.data)

        c.credentials(HTTP_AUTHORIZATION=f'Bearer {r2.data["access"]}')
        self.assertEqual(c.get('/api/users/me/').data['email'], 'a@t.com')

    def test_bad_credentials_and_bad_refresh(self):
        c = APIClient()
        self.assertEqual(c.post('/api/users/login/', {'email': 'a@t.com', 'password': 'nope'}, format='json').status_code, 401)
        self.assertEqual(c.post('/api/users/token/refresh/', {'refresh': 'garbage'}, format='json').status_code, 401)