"""Public MK pages: no login, reached only through the emailed link.

Authentication is switched off on purpose (a stale or foreign Authorization header must not
make the page fail), and only what the MK needs to judge the permit is returned: no user ids
or emails, no storage paths. Unknown links answer 404 for every action.
"""
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import mk, services
from .forms import FORMS
from .models import PermitApproval, PermitPhoto


class OTPSerializer(serializers.Serializer):
    otp = serializers.RegexField(r'^\d{6}$', error_messages={'invalid': 'The code is six digits.'})


class ApproveSerializer(OTPSerializer):
    full_name = serializers.CharField(min_length=2, max_length=150)     # who is deciding, for the record
    remarks = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class RejectSerializer(ApproveSerializer):
    remarks = serializers.CharField(max_length=1000)                    # a reason is required


def permit_payload(permit):
    activity = permit.hira_activity
    photos = list(permit.photos.filter(stage=PermitPhoto.Stage.BEFORE))
    urls = services.signed_urls([p.storage_path for p in photos]) if photos else {}
    rnd = permit.submission_round
    return {
        'id': permit.pk,
        'project': permit.project.name,
        'permit_type': permit.permit_type,
        'permit_type_label': permit.get_permit_type_display(),
        'document_number': permit.document_number,
        'work_date': permit.work_date, 'work_start': permit.work_start, 'work_end': permit.work_end,
        'work_location': permit.work_location, 'work_description': permit.work_description,
        'equipment_used': permit.equipment_used, 'person_in_charge': permit.person_in_charge,
        'worker_count': permit.worker_count,
        'safety_areas': permit.safety_areas, 'checklist': permit.checklist,
        'ppe': permit.ppe, 'ppe_other': permit.ppe_other,
        'risk_category': permit.risk_category,
        'applicant': permit.created_by.full_name,
        'hira_activity': {
            'name': activity.activity_name, 'hazard': activity.potential_hazard,
            'risk_impact': activity.risk_impact, 'initial_score': activity.initial_score,
            'initial_category': activity.initial_category,
        },
        'form': FORMS[permit.permit_type],   # labels for the checklist, areas and PPE
        'approvals': [{
            'step': a.step, 'decision': a.decision, 'remarks': a.remarks, 'decided_at': a.decided_at,
            'decided_by': a.decided_by.full_name if a.decided_by_id else a.external_name,
        } for a in permit.approvals.select_related('decided_by') if a.submission_round == rnd],
        'geo_stamps': [{
            'step': g.step, 'latitude': g.latitude, 'longitude': g.longitude,
            'accuracy': g.accuracy, 'captured_at': g.captured_at,
        } for g in permit.geo_stamps.all() if g.submission_round == rnd],
        'photos': [{'id': p.pk, 'stage': p.stage, 'url': urls.get(p.storage_path)} for p in photos],
    }


class MKPublicView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]


class MKDetailView(MKPublicView):
    throttle_scope = 'mk_read'

    def get(self, request, token):
        req = mk.lookup(token)
        permit = req.permit
        return Response({
            'state': mk.page_state(req, permit),
            'contact_name': req.contact.full_name,
            'expires_at': req.expires_at,
            'otp': mk.otp_state(req),
            'permit': permit_payload(permit),
        })


class MKSendOTPView(MKPublicView):
    throttle_scope = 'mk_write'

    def post(self, request, token):
        return Response({'otp': mk.send_otp(token, request)})


class MKApproveView(MKPublicView):
    throttle_scope = 'mk_write'
    serializer_class = ApproveSerializer
    decision = PermitApproval.Decision.APPROVED

    def post(self, request, token):
        body = self.serializer_class(data=request.data)
        body.is_valid(raise_exception=True)
        d = body.validated_data
        permit = mk.decide(token, request, self.decision, d['otp'], d['full_name'].strip(),
                           d.get('remarks', '').strip())
        return Response({'decision': self.decision, 'permit_status': permit.status})


class MKRejectView(MKApproveView):
    serializer_class = RejectSerializer
    decision = PermitApproval.Decision.REJECTED
