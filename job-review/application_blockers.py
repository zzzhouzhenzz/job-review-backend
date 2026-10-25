"""Validate recorded application blockers. No scheduling or model calls."""
import json
from store import ReviewError

BLOCKER_OPERATIONS = frozenset({'sign_in', 'challenge', 'resume_upload', 'draft_load',
                               'form_navigation', 'form_edit', 'unknown'})


def blocker_operation(reason, operation=None):
    """Keep the original operation distinct from a later recovery observation.

    Only specific reason codes establish a legacy operation. Free-form details
    and a visible attachment cannot classify a generic permission failure.
    """
    implied = {'login_required': 'sign_in', 'challenge_required': 'challenge',
               'application_page_transient': 'draft_load'}.get(reason)
    if operation in (None, ''):
        return implied or 'unknown'
    if operation not in BLOCKER_OPERATIONS or (implied and operation != implied):
        raise ReviewError('The blocked operation does not match its reason.')
    return operation


def latest_application_blocker(db, process_id):
    row = db.execute("""SELECT id,data FROM wf_process_events WHERE process_id=?
        AND type='blocked' ORDER BY id DESC LIMIT 1""", (process_id,)).fetchone()
    return {**json.loads(row['data']), 'id': row['id']} if row else {}
