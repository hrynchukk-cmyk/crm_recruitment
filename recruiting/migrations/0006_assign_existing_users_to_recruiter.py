from django.db import migrations

RECRUITER_GROUP = "Рекрутер"


def forwards(apps, schema_editor):
    """Наявні користувачі без ролі (не адміністратори) стають рекрутерами — щоб не втратили доступ.

    Самі права групи виставляє post_migrate (recruiting.roles.ensure_roles), коли дозволи вже створені.
    """
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("auth", "User")
    group, _ = Group.objects.get_or_create(name=RECRUITER_GROUP)
    for user in User.objects.filter(is_superuser=False):
        user.groups.add(group)


class Migration(migrations.Migration):
    dependencies = [
        ("recruiting", "0005_workua_api_sync"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
