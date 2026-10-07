from app.services.secrets import MANAGED_LABEL, is_managed


def test_resources_from_before_the_rename_are_still_ours():
    assert is_managed({MANAGED_LABEL: "splitwave"})
    assert is_managed({MANAGED_LABEL: "devsecops-platform"})       # создано старой версией: обновление не должно ломать выкат
    assert not is_managed({MANAGED_LABEL: "helm"}) and not is_managed({}) and not is_managed(None)
