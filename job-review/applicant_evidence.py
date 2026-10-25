"""Fresh applicant source snapshots and source-checked answer proposals.

Source text is private worker input. Citations prove provenance, not that an
inference is correct; the question review instructions define that boundary.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from store import ReviewError, digest, text

MAX_SOURCE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 768 * 1024


class EvidenceChanged(ReviewError):
    def __init__(self):
        super().__init__('Applicant evidence changed. Review the current sources again.', 409,
                         reason='applicant_evidence_changed')


def _read(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError('Source is too large')
        content = raw.decode('utf-8')
        if not content.strip() or '\x00' in content:
            raise ValueError('Source is empty or binary')
    except (OSError, ValueError) as error:
        # Do not expose source contents or OS error paths through public errors.
        raise ReviewError('A configured applicant text source cannot be read completely.') from error
    return raw, content


class EvidenceSnapshot:
    def __init__(self, profile, evidence_files, application_index=None):
        entries = [('profile', 'confirmed_profile', Path(profile))] if profile is not None else []
        entries += [(f'resume_text_{i + 1}', 'applicant_evidence', Path(path))
                    for i, path in enumerate(evidence_files)]
        if application_index is not None:
            entries.append(('application_history', 'application_history', Path(application_index)))
        if not entries:
            raise ReviewError('Applicant evidence is required.')
        self.sources = {}
        total = 0
        for source_id, kind, path in entries:
            path = path.resolve()
            raw, content = _read(path)
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                raise ReviewError('Configured applicant evidence exceeds the total size limit.')
            self.sources[source_id] = {'source_id': source_id, 'kind': kind, 'path': str(path),
                                       'sha256': hashlib.sha256(raw).hexdigest(), 'content': content}
        self.hash = digest([{k: source[k] for k in ('source_id', 'kind', 'path', 'sha256')}
                            for source in self.sources.values()])

    def context(self):
        return {'snapshot_hash': self.hash, 'sources': [
            {**{k: source[k] for k in ('source_id', 'kind', 'path', 'sha256')},
             'numbered_text': '\n'.join(f'{i}: {line}' for i, line in enumerate(source['content'].splitlines(), 1))}
            for source in self.sources.values()]}

    def reference_context(self):
        """Browser workers can read exact existing files only when needed.

        Tool-free question review must continue to use context(), because it
        cannot read these paths. The host retains the same freshness fence.
        """
        return {'snapshot_hash': self.hash, 'sources': [
            {**{k: source[k] for k in ('source_id', 'kind', 'path', 'sha256')},
             'bytes': len(source['content'].encode('utf-8')),
             'lines': len(source['content'].splitlines())}
            for source in self.sources.values()]}

    def assert_current(self):
        for source in self.sources.values():
            raw, _ = _read(Path(source['path']))
            if hashlib.sha256(raw).hexdigest() != source['sha256']:
                raise EvidenceChanged()

    def citation(self, value):
        if not isinstance(value, dict):
            raise ReviewError('Answer evidence requires a source citation.')
        source = self.sources.get(value.get('source_id'))
        start, end = value.get('start_line'), value.get('end_line')
        if (source is None or value.get('sha256') != source['sha256'] or
                type(start) is not int or type(end) is not int or not 1 <= start <= end or end - start >= 100):
            raise ReviewError('Answer evidence refers to an invalid source or line range.')
        lines = source['content'].splitlines()
        quote = '\n'.join(lines[start - 1:end])
        if end > len(lines) or not quote.strip() or value.get('quote') != quote:
            raise ReviewError('Answer evidence does not match the cited source text.')
        return {k: value[k] for k in ('source_id', 'sha256', 'start_line', 'end_line', 'quote')}

    def validate_review(self, result, questions, bindings):
        """Validate full review coverage, current identities, values, and citations."""
        current = {question['id']: question for question in questions if question.get('answer') is None}
        resolutions = result.get('resolutions') if isinstance(result, dict) else None
        if not isinstance(resolutions, list) or len(resolutions) != len(current):
            raise ReviewError('Evidence review must cover each unanswered question exactly once.')
        seen, answers, provenance, unresolved = set(), [], {}, []
        for resolution in resolutions:
            if not isinstance(resolution, dict):
                raise ReviewError('Each evidence resolution must be an object.')
            qid = resolution.get('question_id')
            question = current.get(qid)
            if question is None or qid in seen or resolution.get('question_revision') != question['revision']:
                raise ReviewError('Evidence review refers to an unknown or stale question.')
            seen.add(qid)
            explanation = text(resolution.get('explanation'), 'Evidence explanation', 5000, required=True)
            try:
                answer = json.loads(resolution['answer_json'])
            except (KeyError, ValueError, TypeError):
                raise ReviewError('Evidence answer must be valid JSON.') from None
            method = resolution.get('method')
            if method == 'unresolved':
                if answer is not None:
                    raise ReviewError('An unresolved question cannot supply an answer.')
                unresolved.append({'question_id': qid, 'reason': explanation})
                continue
            if method not in {'direct', 'derived', 'drafted'}:
                raise ReviewError('Unsupported evidence answer method.')
            if not isinstance(bindings.get(qid), str) or not bindings[qid]:
                raise ReviewError('Evidence answer is missing its original form field binding.')
            citations = resolution.get('citations')
            if not isinstance(citations, list) or not 1 <= len(citations) <= 20:
                raise ReviewError('A resolved answer requires bounded source citations.')
            citations = [self.citation(item) for item in citations]
            if all(self.sources[item['source_id']]['kind'] == 'application_history' for item in citations):
                raise ReviewError('Application history alone does not confirm a reusable personal answer.')
            kind, choices = question['type'], question.get('choices', [])
            allowed = {choice['id'] for choice in choices}
            if kind == 'text':
                text(answer, 'Evidence answer', 20000, required=True)
            elif kind == 'single_choice':
                if not isinstance(answer, str) or answer not in allowed:
                    raise ReviewError('Evidence answer is not an observed choice.')
            elif kind == 'multi_choice':
                if (not isinstance(answer, list) or not all(isinstance(item, str) for item in answer)
                        or len(set(answer)) != len(answer) or not set(answer) <= allowed
                        or question.get('required') and not answer):
                    raise ReviewError('Evidence answer is not an observed choice set.')
            else:
                raise ReviewError('Unsupported evidence question type.')
            answers.append({'question_id': qid, 'question_revision': question['revision'], 'value': answer})
            provenance[qid] = {'field_name': bindings[qid], 'method': method,
                               'source_snapshot_hash': self.hash, 'citations': citations,
                               'explanation': explanation}
        self.assert_current()
        return answers, provenance, unresolved
