from django.conf import settings
from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import Q

from hira.models import HIRAActivity
from projects.models import Project

User = settings.AUTH_USER_MODEL


class Permit(models.Model):

    class Type(models.TextChoices):
        WORKING_AT_HEIGHT = 'working_at_height', 'Ijin Kerja Di Ketinggian'
        CONFINED_SPACE    = 'confined_space',    'Ijin Kerja Ruang Terbatas'
        FIRE_EXPLOSIVE    = 'fire_explosive',    'Ijin Kerja Berpotensi Kebakaran Dan Ledakan'
        LIFTING           = 'lifting',           'Ijin Kerja Pekerjaan Mengangkat'
        TOWER_CRANE       = 'tower_crane',       'Ijin Kerja Instalasi Dan Segmen Tower Crane'

    class Status(models.TextChoices):
        DRAFT              = 'draft',              'Draft'
        PENDING_SUPERVISOR = 'pending_supervisor', 'Menunggu Supervisor'
        PENDING_OFFICER    = 'pending_officer',    'Menunggu HSE Officer'
        PENDING_PM         = 'pending_pm',         'Menunggu Project Manager'
        PENDING_MK         = 'pending_mk',         'Menunggu Manajemen Konstruksi'
        APPROVED           = 'approved',           'Disetujui'
        REJECTED           = 'rejected',           'Ditolak'
        PENDING_CLOSE      = 'pending_close',      'Menunggu Konfirmasi Penutupan'
        CLOSED             = 'closed',             'Ditutup'
        CANCELLED          = 'cancelled',          'Dibatalkan'

    project       = models.ForeignKey(Project, on_delete=models.PROTECT, related_name='permits')
    hira_activity = models.ForeignKey(HIRAActivity, on_delete=models.PROTECT, related_name='permits')
    permit_type   = models.CharField(max_length=30, choices=Type.choices)
    # Entered manually by HSE Officer / Coordinator (same convention as HIRA).
    document_number = models.CharField(max_length=100, blank=True)

    # Form header
    work_date        = models.DateField()
    work_start       = models.TimeField()
    work_end         = models.TimeField()
    work_location    = models.CharField(max_length=255)
    work_description = models.CharField(max_length=500)
    equipment_used   = models.TextField(blank=True)
    person_in_charge = models.CharField(max_length=255)    # "Yang bertanggung jawab"
    worker_count     = models.PositiveSmallIntegerField()

    # Checklists. Allowed keys come from permits/forms.py.
    safety_areas = models.JSONField(default=list, blank=True)   # ["A", "C"]
    checklist    = models.JSONField(default=dict, blank=True)   # {"1": true, ..., "14": false}
    ppe          = models.JSONField(default=list, blank=True)   # ["body_harness", "sepatu"]
    ppe_other    = models.JSONField(default=list, blank=True)   # free-text lines, max 2

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    submission_round = models.PositiveSmallIntegerField(default=0)
    # Copied from the HIRA activity's initial category at (re)submit. Fixes the chain.
    risk_category = models.CharField(max_length=1, choices=HIRAActivity.RiskCategory.choices,
                                     blank=True, editable=False)

    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='permits_created')

    cancelled_by  = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name='permits_cancelled')
    cancelled_at  = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True)

    post_work_evaluation = models.TextField(blank=True)
    close_requested_at   = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name='permits_closed')
    closed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_permit_type_display()} — {self.work_location} ({self.status})'

    def get_audit_project(self):
        return self.project


class PermitApproval(models.Model):
    """Append-only record of each approver decision."""

    class Step(models.TextChoices):
        SUPERVISOR = 'supervisor',      'Supervisor Lapangan'
        OFFICER    = 'hse_officer',     'HSE Officer'
        PM         = 'project_manager', 'Project Manager'
        MK         = 'mk',              'Manajemen Konstruksi'   # used when the OTP flow lands

    class Decision(models.TextChoices):
        APPROVED = 'approved', 'Disetujui'
        REJECTED = 'rejected', 'Ditolak'

    permit           = models.ForeignKey(Permit, on_delete=models.PROTECT, related_name='approvals')
    step             = models.CharField(max_length=20, choices=Step.choices)
    decision         = models.CharField(max_length=10, choices=Decision.choices)
    decided_by       = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True,
                                         related_name='permit_decisions')   # null for MK (no account)
    remarks          = models.TextField(blank=True)
    submission_round = models.PositiveSmallIntegerField()
    decided_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['decided_at']
        constraints = [
            models.CheckConstraint(condition=Q(decision='approved') | ~Q(remarks=''),
                                   name='permit_rejection_requires_remarks'),
            models.UniqueConstraint(fields=['permit', 'step', 'submission_round'],
                                    name='uniq_permit_decision_per_step_round'),
        ]

    def __str__(self):
        return f'{self.permit_id} {self.step} {self.decision}'

    def get_audit_project(self):
        return self.permit.project


class PermitPhoto(models.Model):
    """Photo evidence. 'before' is attached by the Subkontraktor before submitting, 'after'
    before requesting closure. The file lives in private storage; only its path is kept."""

    class Stage(models.TextChoices):
        BEFORE = 'before', 'Sebelum pekerjaan'
        AFTER  = 'after',  'Setelah pekerjaan'

    permit        = models.ForeignKey(Permit, on_delete=models.CASCADE, related_name='photos')
    stage         = models.CharField(max_length=10, choices=Stage.choices)
    storage_path  = models.CharField(max_length=255, unique=True)
    original_name = models.CharField(max_length=255, blank=True)
    content_type  = models.CharField(max_length=50)
    size          = models.PositiveIntegerField()
    uploaded_by   = models.ForeignKey(User, on_delete=models.PROTECT, related_name='permit_photos')
    uploaded_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['uploaded_at', 'id']
        indexes = [models.Index(fields=['permit', 'stage'])]

    def __str__(self):
        return f'{self.permit_id} {self.stage} {self.storage_path}'

    def get_audit_project(self):
        return self.permit.project


class PermitGeoStamp(models.Model):
    """Where the device said it was when someone acted. Self-reported by the browser and
    checked by a human in the approval chain, so it is stored as given, not verified."""

    class Step(models.TextChoices):
        CREATOR    = 'creator',     'Subkontraktor (pengajuan)'
        SUPERVISOR = 'supervisor',  'Supervisor Lapangan'
        OFFICER    = 'hse_officer', 'HSE Officer'

    permit           = models.ForeignKey(Permit, on_delete=models.PROTECT, related_name='geo_stamps')
    step             = models.CharField(max_length=20, choices=Step.choices)
    submission_round = models.PositiveSmallIntegerField()
    latitude         = models.FloatField(validators=[MinValueValidator(-90), MaxValueValidator(90)])
    longitude        = models.FloatField(validators=[MinValueValidator(-180), MaxValueValidator(180)])
    accuracy         = models.FloatField(validators=[MinValueValidator(0)])   # metres
    captured_at      = models.DateTimeField()                                 # device clock
    recorded_by      = models.ForeignKey(User, on_delete=models.PROTECT, related_name='permit_geo_stamps')
    recorded_at      = models.DateTimeField(auto_now_add=True)               # server clock

    class Meta:
        ordering = ['recorded_at', 'id']
        constraints = [
            models.UniqueConstraint(fields=['permit', 'step', 'submission_round'],
                                    name='uniq_permit_geo_per_step_round'),
            models.CheckConstraint(condition=Q(latitude__gte=-90, latitude__lte=90,
                                               longitude__gte=-180, longitude__lte=180,
                                               accuracy__gte=0),
                                   name='permit_geo_in_range'),
        ]

    def __str__(self):
        return f'{self.permit_id} {self.step} {self.latitude:.5f},{self.longitude:.5f}'
