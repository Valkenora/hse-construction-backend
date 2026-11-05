"""The words of every notification, in one place."""
from audit.labels import clean_remarks, resource_label, resource_title, summary

TYPES = {'permits.permit': 'permit', 'hira.hiradocument': 'hira'}
LABEL_KEYS = {'permit': 'permits.permit', 'hira': 'hira.hiradocument'}

RESOLVED = {                     # audit action -> how a handled "needs action" notice reads afterwards
    'approved': 'Sudah disetujui',
    'rejected': 'Sudah ditolak',
    'closed': 'Penutupan sudah dikonfirmasi',
    'cancelled': 'Dibatalkan',
}


def category(rtype):
    return resource_label(LABEL_KEYS[rtype])


def doc_title(rtype, resource):
    if rtype == 'permit':
        return resource_title('permits.permit', {'document_number': resource.document_number,
                                                 'work_description': resource.work_description}, resource.pk)
    return resource_title('hira.hiradocument', {'document_number': resource.document_number,
                                                'work_package': resource.work_package}, resource.pk)


def describe(rtype, resource):
    return f'{category(rtype)} {doc_title(rtype, resource)} ({resource.project.name})'


def action_needed(rtype, resource, status):
    what = 'Menunggu konfirmasi penutupan Anda' if status == 'pending_close' else 'Menunggu persetujuan Anda'
    again = ' Diajukan ulang.' if resource.submission_round > 1 else ''
    return what, describe(rtype, resource) + '.' + again


def submitted(rtype, resource):
    return 'Dokumen baru diajukan', f'{describe(rtype, resource)}, oleh {resource.created_by.full_name}.'


def approved(rtype, resource):
    return 'Disetujui sepenuhnya', describe(rtype, resource) + '.'


def rejected(rtype, resource, entry):
    return 'Ditolak', f'{describe(rtype, resource)}: {summary("rejected", entry.remarks, entry.snapshot)}'.rstrip(': ')


def closed(rtype, resource):
    return 'Izin kerja ditutup', describe(rtype, resource) + '.'


def cancelled(rtype, resource, entry):
    reason = clean_remarks('cancelled', entry.remarks)
    return 'Dibatalkan', describe(rtype, resource) + (f': {reason}' if reason else '.')
