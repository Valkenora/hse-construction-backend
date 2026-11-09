from django.db import migrations


def nothing(apps, schema_editor):
    """Forwards there is nothing to copy: the activities already say which documents are involved."""


def restore_document(apps, schema_editor):
    """Going back: a permit gets the document of its first linked activity (the old column held one)."""
    Permit = apps.get_model('permits', 'Permit')
    Link = apps.get_model('permits', 'PermitHIRAActivity')
    for permit in Permit.objects.all():
        first = (Link.objects.filter(permit=permit).select_related('hira_activity__group')
                 .order_by('pk').first())
        if first is not None:
            Permit.objects.filter(pk=permit.pk).update(
                hira_document_id=first.hira_activity.group.hira_document_id)


class Migration(migrations.Migration):

    dependencies = [
        ('permits', '0006_remove_single_hira_activity'),
    ]

    operations = [
        migrations.RunPython(nothing, restore_document),
        migrations.RemoveField(model_name='permit', name='hira_document'),
    ]
