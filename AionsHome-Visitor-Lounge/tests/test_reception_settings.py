from dataclasses import replace

import pytest

from visitor_lounge.reception_settings import (
    InvalidReceptionSettings,
    ReceptionSettingsRepository,
)


def _repository(database, tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "persona.md").write_text("温和而稳重。", encoding="utf-8")
    database.initialize()
    return ReceptionSettingsRepository(database, tmp_path)


def test_reception_settings_default_to_local_persona_and_thirty_minutes(
    database, tmp_path
):
    settings = _repository(database, tmp_path).get()

    assert settings.persona_text == "温和而稳重。"
    assert settings.idle_minutes == 30
    assert settings.lounge_enabled is True
    assert "{访客名字}" in settings.first_welcome
    assert "{访客名字}" in settings.returning_welcome


def test_reception_settings_save_atomically_and_restore_defaults(database, tmp_path):
    repository = _repository(database, tmp_path)
    original = repository.get()

    saved = repository.save(
        replace(original, persona_text="新的全局人设", first_welcome="你好，{访客名字}")
    )

    assert repository.get() == saved
    assert saved.persona_text == "新的全局人设"
    assert repository.restore_defaults().persona_text == "温和而稳重。"


def test_reception_settings_reject_unknown_template_placeholder(database, tmp_path):
    repository = _repository(database, tmp_path)
    settings = repository.get()

    with pytest.raises(InvalidReceptionSettings):
        repository.save(replace(settings, first_welcome="你好，{未知字段}"))

    assert repository.get() == settings
