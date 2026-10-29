from django.conf import settings
from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import Q

from hira.models import HIRAActivity, HIRADocument
from projects.models import Project, ProjectMKContact

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
    # The HIRA this permit rests on: one approved document of the creator's, and up to 10 of its
    # activities (from any group). The document may be chosen after the permit is created.
    hira_document = models.ForeignKey(HIRADocument, on_delete=models.PROTECT, null=True, blank=True,
                                      related_name='permits')
    hira_activities = models.ManyToManyField(HIRAActivity, through='PermitHIRAActivity',
                                             related_name='permits', blank=True)
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

    def audit_extra(self):
        """Added to every audit snapshot, so history shows which activities were linked."""
        if self.pk is None:
            return {}
        return {'hira_activity_ids': sorted(self.hira_activities.values_list('pk', flat=True))}


class PermitHIRAActivity(models.Model):
    """One HIRA activity linked to a permit."""
    permit        = models.ForeignKey(Permit, on_delete=models.CASCADE, related_name='hira_links')
    hira_activity = models.ForeignKey(HIRAActivity, on_delete=models.PROTECT, related_name='permit_links')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['permit', 'hira_activity'], name='uniq_activity_per_permit'),
        ]

    def __str__(self):
        return f'{self.permit_id} -> activity {self.hira_activity_id}'


class PermitApproval(models.Model):
    """Append-only record of each approver decision."""

    class Step(models.TextChoices):
        SUPERVISOR = 'supervisor',      'Supervisor Lapangan'
        OFFICER    = 'hse_officer',     'HSE Officer'
        PM         = 'project_manager', 'Project Manager'
        MK         = 'mk',              'Manajemen Konstruksi'   # decided via emailed link + OTP

    class Decision(models.TextChoices):
        APPROVED = 'approved', 'Disetujui'
        REJECTED = 'rejected', 'Ditolak'

    permit           = models.ForeignKey(Permit, on_delete=models.PROTECT, related_name='approvals')
    step             = models.CharField(max_length=20, choices=Step.choices)
    decision         = models.CharField(max_length=10, choices=Decision.choices)
    decided_by       = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True,
                                         related_name='permit_decisions')   # null for MK (no account)
    # MK decides through an emailed link + OTP, so there is no user. These record who it was.
    mk_contact       = models.ForeignKey(ProjectMKContact, on_delete=models.PROTECT, null=True, blank=True,
                                         related_name='decisions')
    external_name    = models.CharField(max_length=150, blank=True)    # typed by the MK at decision time
    external_email   = models.EmailField(blank=True)
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
            models.CheckConstraint(condition=Q(decided_by__isnull=False) | Q(step='mk'),
                                   name='permit_decider_required_unless_mk'),
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



class MKApprovalRequest(models.Model):
    """One emailed link for one MK contact for one submission round. The link token and the
    OTP are never stored, only their hashes. Any one contact deciding closes the others."""

    class Status(models.TextChoices):
        ACTIVE     = 'active',     'Aktif'
        DECIDED    = 'decided',    'Sudah diputuskan'
        SUPERSEDED = 'superseded', 'Digantikan'    # a newer link was issued, or another contact decided

    permit           = models.ForeignKey(Permit, on_delete=models.PROTECT, related_name='mk_requests')
    contact          = models.ForeignKey(ProjectMKContact, on_delete=models.PROTECT, related_name='requests')
    submission_round = models.PositiveSmallIntegerField()
    status           = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    token_hash       = models.CharField(max_length=64, unique=True, editable=False)
    expires_at       = models.DateTimeField()

    email_sent_at = models.DateTimeField(null=True, blank=True)
    email_error   = models.CharField(max_length=255, blank=True)   # why the last send failed

    otp_hash       = models.CharField(max_length=64, blank=True, editable=False)
    otp_expires_at = models.DateTimeField(null=True, blank=True)
    otp_sent_at    = models.DateTimeField(null=True, blank=True)
    otp_sent_count = models.PositiveSmallIntegerField(default=0)
    otp_attempts   = models.PositiveSmallIntegerField(default=0)   # wrong tries on the current code

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['permit', 'status'])]

    def __str__(self):
        return f'MK link #{self.pk} permit {self.permit_id} {self.contact.email} ({self.status})'

    def get_audit_project(self):
        return self.permit.project



class PermitAttachment(models.Model):
    """A PDF the approvers read: the workers' fit-to-work (medical) document and their
    work-at-height certificate. Evidence only: not tied to individual workers, and the system
    does not check expiry; the approvers judge that. Private storage; only the path is kept."""

    class Kind(models.TextChoices):
        MEDICAL     = 'medical',     'Surat keterangan sehat (fit to work)'
        CERTIFICATE = 'certificate', 'Sertifikat kompetensi'

    permit        = models.ForeignKey(Permit, on_delete=models.CASCADE, related_name='attachments')
    kind          = models.CharField(max_length=12, choices=Kind.choices)
    storage_path  = models.CharField(max_length=255, unique=True)
    original_name = models.CharField(max_length=255, blank=True)
    size          = models.PositiveIntegerField()
    uploaded_by   = models.ForeignKey(User, on_delete=models.PROTECT, related_name='permit_attachments')
    uploaded_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['uploaded_at', 'id']
        indexes = [models.Index(fields=['permit', 'kind'])]

    def __str__(self):
        return f'{self.permit_id} {self.kind} {self.original_name}'

    def get_audit_project(self):
        return self.permit.project
