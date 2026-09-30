from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', 'hse_coordinator')
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):

    class Role(models.TextChoices):
        SUBKONTRAKTOR       = 'subkontraktor',      'Subkontraktor'
        SUPERVISOR_LAPANGAN = 'supervisor_lapangan', 'Supervisor Lapangan / Koordinator Supervisor'
        HSE_OFFICER         = 'hse_officer',         'HSE Officer'
        HSE_COORDINATOR     = 'hse_coordinator',     'HSE Coordinator'
        PROJECT_MANAGER     = 'project_manager',     'Project Manager'

    email       = models.EmailField(unique=True)
    full_name   = models.CharField(max_length=150)
    role        = models.CharField(max_length=30, choices=Role.choices)
    is_active   = models.BooleanField(default=True)
    is_staff    = models.BooleanField(default=False)
    created_at  = models.DateTimeField(auto_now_add=True)

    groups = models.ManyToManyField(
        'auth.Group',
        blank=True,
        related_name='custom_user_set'
    )
    user_permissions = models.ManyToManyField(
        'auth.Permission',
        blank=True,
        related_name='custom_user_set'
    )

    objects = UserManager()

    USERNAME_FIELD  = 'email'
    REQUIRED_FIELDS = ['full_name', 'role']

    def __str__(self):
        return f'{self.full_name} ({self.role})'

    @property
    def is_super_user_role(self):
        """HSE Coordinator and HSE Officer have cross-project visibility."""
        return self.role in [self.Role.HSE_COORDINATOR, self.Role.HSE_OFFICER]