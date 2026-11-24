from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workload', '0024_remove_classschedule_workload_cl_faculty_0ddba7_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='facultyprofile',
            name='academic_rank',
            field=models.CharField(
                blank=True,
                default='',
                help_text='e.g. Instructor I, Assistant Professor II. Blank by default.',
                max_length=100,
            ),
        ),
    ]
