"""Reuse exact user-confirmed questions before asking a model to interpret them.

Only explicit portable question forms cross employer boundaries automatically.
Other equivalent wording remains the evidence reviewer's responsibility.
"""
import json
import re
import unicodedata


def normalize(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


PORTABLE = {normalize(value) for value in (
    'Gender', 'Gender identity', 'Race and Ethnicity', 'Race/Ethnicity',
    'Are you Hispanic/Latino?', 'Veteran Status', 'Disability Status',
    'Are you at least 18 years of age?', 'Full legal name', 'First name', 'Last name',
    'Email', 'Email address', 'Phone number', 'Current city',
    'Are you authorized to work in the United States?',
    'Are you authorized to work in the US for any employer?',
    'Will you now or in the future require US employment sponsorship?',
)}


def reuse_scope(question_text):
    normalized = normalize(question_text)
    if normalized in PORTABLE:
        return 'personal'
    # A legal statement or role-specific promise needs its original context.
    if re.search(r'consent|certify|acknowledg|signature|privacy|contact|authoriz|agree|relocat', normalized):
        return 'application'
    if re.search(r'worked for|employed by|employment with', normalized):
        return 'company'
    return 'application'


def matches_context(record, process, scope):
    saved_scope=record.get('reuse_scope')
    if saved_scope is not None:
        if not isinstance(saved_scope,dict) or set(saved_scope) != {'kind','key'}:
            return False
        kind,key=saved_scope['kind'],saved_scope['key']
        if kind == 'application' and key != process['id']:
            return False
        if kind == 'company' and key != process['snapshot']['company_id']:
            return False
        if kind not in {'application','company','personal'} or kind == 'personal' and key != '':
            return False
    if scope == 'personal':
        return True
    if record.get('company_id') != process['snapshot']['company_id']:
        return False
    if scope == 'company':
        return True
    return (record.get('process_id') == process['id'] and
            record.get('form_version') == process.get('form_version'))


def map_value(record, question):
    if question['type'] != record['question_type']:
        return None
    if question['type'] == 'text':
        return record['value']
    old_labels = [normalize(choice['label']) for choice in record['choices']]
    new_labels = [normalize(choice['label']) for choice in question['choices']]
    # Matching the whole set avoids equating short headings with different
    # legal definitions or disclosure options. IDs are local to each form.
    if (len(old_labels) != len(set(old_labels)) or len(new_labels) != len(set(new_labels))
            or set(old_labels) != set(new_labels)):
        return None
    ids = dict(zip(new_labels, [choice['id'] for choice in question['choices']]))
    selected = [normalize(label) for label in record['answer_labels']]
    if any(label not in ids for label in selected):
        return None
    if question['type'] == 'single_choice':
        return ids[selected[0]] if len(selected) == 1 else None
    return [ids[label] for label in selected]


def saved_resolutions(evidence, process, questions):
    """Return only exact, scoped matches with citations to the current profile."""
    from answer_profile import read_profile_records
    source = evidence.sources.get('profile')
    if source is None:
        return []
    records = read_profile_records(source['content'])
    resolutions = []
    for question in questions:
        scope = reuse_scope(question['text'])
        eligible = [(record, line) for record, line in records
                    if record.get('authority') == 'user'
                    and normalize(record['question_text']) == normalize(question['text'])
                    and matches_context(record, process, scope)]
        if not eligible:
            continue
        # A newer correction must never fall back to an older matching option.
        record, line = max(eligible, key=lambda pair: (pair[0]['source_request_sequence'], pair[0]['record_id']))
        value = map_value(record, question)
        if value is None:
            continue
        resolutions.append({'question_id': question['id'], 'question_revision': question['revision'],
            'answer_json': json.dumps(value, ensure_ascii=False), 'method': 'direct',
            'citations': [{'source_id': 'profile', 'sha256': source['sha256'],
                          'start_line': line, 'end_line': line,
                          'quote': source['content'].splitlines()[line - 1]}],
            'explanation': f"Reused user answer record {record['record_id']} from request {record['request_id']}; "
                           f"same question and {scope} context, latest correction, current choice labels."})
    return resolutions
