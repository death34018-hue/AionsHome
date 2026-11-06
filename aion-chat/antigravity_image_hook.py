"""AGY PreToolUse policy: approve only this turn's staged image/audio attachments.

The hook and manifest keep their existing names for installed chat workspaces.
"""

import json
import os
import sys
from pathlib import Path


def is_allowed_attachment_read(name, parameters, image_paths):
    # AGY 1.5.2 renamed view_file(AbsolutePath) to read_file(Path).
    path_key = {'view_file': 'AbsolutePath', 'read_file': 'Path'}.get(name)
    if not path_key or not isinstance(parameters, dict):
        return False
    path = parameters.get(path_key)
    if not isinstance(path, str) or not path:
        return False
    allowed = {os.path.normcase(str(Path(item).resolve())) for item in image_paths}
    return os.path.normcase(str(Path(path).resolve())) in allowed


def image_permission_decision(event, image_paths):
    call = event.get('toolCall') or {}
    if is_allowed_attachment_read(call.get('name'), call.get('args'), image_paths):
        # CLI 1.2.16 still consumes the legacy allowTool/denyReason fields.
        return {'decision': 'allow', 'allowTool': True}
    reason = 'Only the current chat image/audio attachments may be read.'
    return {'decision': 'deny', 'allowTool': False, 'reason': reason, 'denyReason': reason}


if __name__ == '__main__':
    try:
        # AGY sends UTF-8 JSON; Windows text stdin may default to GBK.
        event = json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))
        image_paths = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
        decision = image_permission_decision(event, image_paths)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        reason = 'Attachment permissions could not be verified.'
        decision = {'decision': 'deny', 'allowTool': False, 'reason': reason, 'denyReason': reason}
    print(json.dumps(decision))
