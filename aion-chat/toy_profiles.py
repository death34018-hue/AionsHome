"""One saved toy selection per home; each controller retains its own protocol."""
from contextvars import ContextVar
from uuid import uuid4
from config import SETTINGS, save_settings

PROFILES = ('sosexy', 'svakom', 'ankni')
_epoch = uuid4().hex
_permission = ContextVar('selected_toy_permission', default=None)


def selected():
    value = SETTINGS.get('toy_profile')
    return value if value in PROFILES else None


def active():
    # Existing homes retain one usable legacy controller until their first selection.
    from capabilities import is_capability_enabled
    return selected() or ('svakom' if is_capability_enabled('svakom') else 'sosexy')


def state():
    return {'profile': selected(), 'active': active(), 'epoch': _epoch}


def select(profile):
    global _epoch
    if profile not in PROFILES:
        raise ValueError('未知玩具')
    if selected() != profile:
        save_settings({**SETTINGS, 'toy_profile': profile})
        SETTINGS['toy_profile'] = profile
        _epoch = uuid4().hex
    return state()


def capture_permission(*, legacy_enabled=False):
    _permission.set((active(), _epoch) if legacy_enabled else None)


def allow_legacy():
    from capabilities import is_capability_enabled
    return active() == 'sosexy' and is_capability_enabled('toy') and _permission.get() == ('sosexy', _epoch)
