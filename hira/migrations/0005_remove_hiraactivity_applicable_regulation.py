from django.db import migrations


class Migration(migrations.Migration):
    """Separate from 0004 on purpose: PostgreSQL does not like data changes and table
    alterations in one transaction. The data was copied to the group in 0004."""

    dependencies = [
        ('hira', '0004_hiragroup_applicable_regulation'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='hiraactivity',
            name='applicable_regulation',
        ),
    ]
